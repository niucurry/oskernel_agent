# 描述流水线的完整性回放

2026-09-27 起建立的工程诊断目录，不是自然语言语义正确性基准。后续生产修复见 [C/Rust 实验报告中的系统修复记录](../../docs/description-scope-c-rust-trial-2026-10-02.md)，当前研究要求见[重新选题交接](../../docs/research-handoff-next-method-2026-10-02.md)。

后续按用户要求完成旧实验清理，见[清理与归档记录](../../docs/retired-experiments-2026-09-27.md)。本目录与回放所需的 17 份摘要保留；旧 CFC 冻结实现改为保存在外部校验归档中。

2026-10-03 更新：后续描述研究原型也已[清理归档](../../docs/retired-description-experiments-2026-10-02.md)，本目录的回放脚本、回归输入和生产修复保留。已不用的先导协议、空白标注模板移入归档；清理后 337 项生产相关测试通过。

## 结果与文件

| 材料 | 结果 | 不能得出的结论 |
|---|---|---|
| `before.json` / `after.json` | 为具体缺陷构造的 8 个用例：旧实现 0/8，新实现 8/8 满足约束 | 自然报告错误率、模型理解能力、论文方法增益 |
| `archive-before.json` / `archive-after.json` | 17 份旧模型摘要、160 条陈述、158 条去重文本：完整文本保留 136 → 160；新实现 5 条放入展开区 | 准确率 85% → 100%、真实生产报告表现、读者一定看到完整内容 |
| `baseline/` | 本轮修改前的三个源码快照，结果 JSON 记录其哈希 | 不包含所有依赖的冻结环境；需配合本项目版本使用 |
| `witnesses.md` | 合成回归用例的输入和前后输出 | 独立测试集 |
| `validation.json` | 测试命令、结果、临时依赖和字体来源记录 | 整仓库所有测试都已通过 |
| `cleanup-scope-result-2026-10-02.json` / `cleanup-scope-validation-2026-10-02.json` | 本次描述研究清理范围、归档校验和 337 项生产回归结果 | 新方法效果或整个仓库测试通过 |

档案回放只抽取包含 `model_summary_unverified` 的 summary.md，在第一个二级标题前的 `- ` 条目。移除同一行明确标记的旧图引用尾注，保留原始行、处理后输入、原文件哈希和完整渲染输出。每次只给卡片一个陈述，不模拟真实报告中多条陈述竞争篇幅。未请独立专家判断源码与这些陈述是否一致。

## 无模型调用的复现

在仓库根目录、项目依赖已可用时运行：

```bash
PYTHONPATH=src:. python3 research/description_integrity/replay.py --baseline --output /tmp/description-before.json
PYTHONPATH=src:. python3 research/description_integrity/replay.py --output /tmp/description-after.json
PYTHONPATH=src:. python3 research/description_integrity/archive_replay.py --baseline --output /tmp/archive-before.json
PYTHONPATH=src:. python3 research/description_integrity/archive_replay.py --output /tmp/archive-after.json
python3 -m pytest -q tests/test_description_integrity.py tests/test_finals_readability.py tests/test_finals_description.py
```

2026-09-27 的报告回归为 **331 passed**，含 PDF 生成。该环境最初缺少已在 requirements.txt 声明的部分依赖；当时为测试将它们安装到 `/tmp/oskernel-description-test-deps`，未改写全局环境或项目依赖声明。PDF 测试通过 `FINALS_CJK_FONT` 和 `FINALS_CJK_BOLD_FONT` 显式设置可嵌入的中文 TrueType 字体。来源为 [Noto CJK 官方仓库](https://github.com/notofonts/noto-cjk/blob/main/Sans/README.md)，确切 commit、字体哈希和静态实例化参数见 validation.json。这个临时环境不是部署安装；实际部署仍需配置兼容字体。

当前测试依赖位于 `/home/niu/.cache/oskernel-description-test-deps`，字体位于 `/home/niu/.cache/oskernel-description-fonts`。清理后 337 项测试的完整环境变量和命令见 [最新验证记录](cleanup-scope-validation-2026-10-02.json)。下述 `/tmp/` 样例路径属于当时的验证记录，不保证临时文件仍存在。

PDF 冒烟样例使用测试夹具，非真实比赛评审；验证为一页 A4、无超链接、可提取 551 字符，另已栅格化检查中英文显示和版面。临时产物位于 `/tmp/oskernel-description-fonts/`。

## 工程取舍

300 字约束现在针对概览正文。较长的完整实现／亮点在 `<details>` 中保留，问题陈述完整展示；三者各有独立的字数属性。完整保留会增加全文长度，也可能降低概览覆盖，这两项代价不能在研究评价中隐去。

仅有 `brief` 的旧产物已丢失的文字无法自动恢复。上游模型误读、跨模块条件遗漏、语义证据不足以及其他 digest/PDF 压缩路径，仍需进一步审查。本轮没有把“原文保留”标成“源码语义已验证”。

旧 CFC 冻结系统的 37 个文件已核对哈希，无不一致；旧负结果和正式留出协议未改动。
