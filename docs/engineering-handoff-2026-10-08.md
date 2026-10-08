# 工程保留与研究范围交接（2026-10-08）

用户最新决定：停止本轮偏离描述系统的研究，保留对系统有益的已有改进并提交远程；新任务只研究代码描述系统。后续请先读 [新任务提示词](description-research-restart-2026-10-08.md)。旧交接中的扩大研究范围、继续内核缺陷或对比方法实验等安排已失效。

没有验证出合适的论文方法。本次提交是工程修复、回归测试与历史记录，不是研究效果或顶刊录用依据。

## 保留的生产改进

- **描述展示和引用**：完整 summary 优先于已经截短的 brief；不再凭共有关键词删掉独立陈述；超出概览预算的实现／亮点以完整文本及对应引用保留在详情；问题文本完整展示。明确区分纯文本和 HTML，避免 `fd<0`、泛型或编码后的比较符被误删或重复解码。不会凭网络关键词补出 smoltcp，也不会凭 ENOSYS 编造函数名。
- **源码取样与 PDF**：文件提示输入按目录／语言轮流覆盖；字体需实际可嵌入且具备基本中英文字形，不兼容字体继续尝试，缺少兼容字体时明确失败。
- **描述证据范围**：源码 SMP 关键词只作线索，避免把蓝牙 Security Manager Protocol 当成多核支持；按 `.sh` 后缀选择 shell 的加载器线索要求核对 ELF 内容优先级，不直接断言违规。
- **运行日志**：新增 `oskernel-finals audit-run`，按最后一轮 RT-Thread utest 的明确边界核对。断言失败却汇总通过标记矛盾；未结束、零测试和无法识别的协议保持未知。原日志 SHA 保留，禁止输出覆盖原日志。日志一致不证明内核能力或来源真实性。
- **开发过程统计**：浅克隆边界无父版本差分时保持未知，合并提交未测量变更量不记为零；未知项不参与异常阈值。作者声明日期以 UTC 范围展示，不推断真实工作日期；不改变原提交身份和顺序。
- **对比系统的已有保护**：路径、向量相似度或局部重合不再证明整个函数来自公共上游。完整当前 C/Rust 源码核验保留名称、常量和字符串，忽略注释／空白；解析失败或参考缺失不能触发整函数排除。L0 文件跳过需核验原文或完整源码指纹，旧索引和旧布尔标签保守处理。本项仅保留工程修复，后续不作为研究方向。

这些修复只解决各自覆盖的问题。详情增加了全文长度，有限源码扫描可能漏检，日志和字体验证也不保证端到端描述正确。

## 本次验证

2026-10-08 实际运行整仓 `pytest tests/`：因环境缺少 `torch`，`tests/test_ai_detect.py` 在收集阶段中断，退出码 2。不能说完整测试套件通过。

显式排除该文件后，离线运行其余测试：**682 passed，7 skipped，2 warnings**，退出码 0。七项跳过为六项依赖 codet5p／torch 的模型测试和一项 Windows 专属测试；两个警告为现有 protobuf 兼容性警告。测试验证工程行为，不验证模型描述质量、竞赛评审效果或论文贡献。

```bash
env PYTHONPATH=/home/niu/.cache/oskernel-description-test-deps:/home/niu/.cache/oskernel-upstream-guard-test-deps:src:. \
  FINALS_CJK_FONT=/home/niu/.cache/oskernel-description-fonts/NotoSansSC-Regular.ttf \
  FINALS_CJK_BOLD_FONT=/home/niu/.cache/oskernel-description-fonts/NotoSansSC-Bold.ttf \
  HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  python3 -m pytest tests/ -q -p no:cacheprovider --ignore=tests/test_ai_detect.py
```

上述依赖和字体是本机缓存；其他机器应按项目依赖安装并配置兼容字体。原运行日志与 JUnit 在交付归档中保留。Git 历史回归所需的有限夹具已放入 `tests/history_controls.py`，不依赖未提交的 `research/history_evidence_audit/`。提交还会从暂存区导出独立副本验证，结果见 [机器可读验证记录](engineering-validation-2026-10-08.json)。

## 提交与本机保留范围

提交生产代码、必要回归测试、新任务提示词、历史结论文档，以及描述展示修复所需的小型回放和旧描述负结果摘要。没有将较大研究原始材料、公开内核克隆、工具链、缓存、数据库或私有模型配置批量加入 Git。

本机 `research/` 与 `/home/niu/agent-archives/` 的历史材料保留；旧文档引用这些路径时，可能需要本机材料或外部归档，远程检出不自动获得所有实验原件。保留清单位于 `/home/niu/agent-archives/description-restart-20261008T021746Z/preservation-before.json`。其中历史实验及原交接保持原内容，根 README 的研究入口更新为描述系统。

`docs/finals-report-plan.md` 和 `docs/progress-presentation.pptx` 的删除是用户原有工作区状态；此次不恢复，也不将两项删除混入工程提交。新任务不得覆盖这些未提交状态。

## 历史研究结论

三条旧描述主线继续停止：行为图／条件单元／状态卡、条件 JSON、压缩条件保持／阶段损失／局部修复。原始负结果见 [完整旧交接](research-handoff-next-method-2026-10-02.md)，不能通过换模型、评测器、提示词或角色再包装。

之后的配置采样、来源残差、身份／运行核验、历史证据、自然上游真值和性能门控等试验没有建立适合本项目的新论文方法；结论的样本、基线和范围分别见已有研究文档。真实缺陷调查只取得官方修复与源文件材料，不能称为自然缺陷复现成功。本次没有继续编译、启动或开展这些方法实验。

这些记录用于避免重复投入。下一任务应从描述系统的实际失败出发，提出新的问题并实际验证，不能把已有工程修复或偏离方向的试验当作新论文的起点。
