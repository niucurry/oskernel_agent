# 最近公开实现的适用性预检

尚未找到合适的新论文方法。本目录核对最近的描述方法及基线，保留可复现证据，不把提取器修复包装为研究贡献。

[RepoDoc 原文](https://arxiv.org/html/2604.26523v1)已经包含实体提取、功能层次组织、精确片段查询和逐层描述生成，因此“把文件换成符号”“做功能聚类”“减少原文重复传输”本身不足以声称新颖。作者仓库 SYSUSELab/RepoDoc 在本次核对时的精确版本是 `306becd0f143211c0dde2bdbd480578356280e28`。11 个下载文件逐一与 GitHub tree 的 Git blob SHA-1 核对，并保存 SHA-256。不是只检索到标题后宣布已经复现。

## 实际运行

`probe.py` 加载作者原样 `TreeSitterCAnalyzer` 与 `Node`/`CallRelationship` 类。只调整运行环境中的绝对导入解析，不编辑作者文件；只保存函数提取结果，丢弃其内部关系结果，不研究行为图或跨调用状态。没有运行生成器、聚类或完整 RepoDoc。

|已暴露公开源码切片|Ctags 定义位置|作者函数位置|交集|
|---|---:|---:|---:|
|ThreadX|230|321|193|
|ChibiOS|162|139|137|
|NuttX|693|649|638|

这里是 `(相对文件, 名称, 起始行)` 的位置一致性，Ctags 也是工具，不是整份 OS 的语义真值。所有差异保存在 `results.json`，不能把差异一律计作作者错误。此运行 0 个模型请求，自身及子进程实测 CPU 合计约 1.086 秒。之前暴露过的这三个仓库不能重新称为盲测。

一个独立可编译 C 片段进一步确认了两类结构性问题。`/usr/bin/gcc-12 -std=c11 -Werror -c` 和 `nm --defined-only` 得到 `allocate/plain/release` 三个代码符号；作者提取器列出 `if/plain/release`，漏掉指针返回函数 `allocate`，多出保留字 `if`。当前生产清单列出正确的三个函数及一个真实结构类型，同时保留 `partial` 状态。源码、编译命令、符号输出和初次找不到 `gcc` 的环境失败分别保存。该片段的编译不等于构建了三份 OS。

## 对后续选择的约束

- 不能只用作者提取器的缺陷做出新方法胜出的结论。需要保留原样结果，并让适用的简单 Ctags 方法参加同信息、同预算比较。
- 真正的语义生成效果仍需独立参考及完整报告。此处尚未生成描述，不能给正确率、评委速度或论文效果定分。
- [SONAR 原文](https://arxiv.org/html/2608.04195v1)已经用再生成代码及执行信号评价摘要；其主要验证是 Python 函数与自动消费任务，不能直接当 OS 模块报告或真人评委效果。摘要压缩部分不在本任务范围，不实施。不能把换成 C/OS 或更换生成模型视作新贡献。
- [完整 oracle 的覆盖评价原文](https://arxiv.org/html/2606.09376v1)已有确定性参考、精度与召回、验证器反馈生成。源码符号清单不能自动成为完整功能 oracle；不能把普通计数换名为完整语义覆盖保证。

复现（不读取私有 API 配置、不发模型请求）：

```sh
cd /home/niu/agent
env PYTHONPATH=/home/niu/.cache/oskernel-description-test-deps:src:. python3 /home/niu/agent-research/description_repodoc_assessment/probe.py
env PYTHONPATH=/home/niu/.cache/oskernel-description-test-deps:src:. python3 /home/niu/agent-research/description_repodoc_assessment/fixture_probe.py
```

公开 OS 输入沿用 `/home/niu/agent-research/description_responsibility_budget/` 的已校验版本与清单，不再复制。`freeze.json` 是此次主预检运行前的协议/实现/作者文件冻结；片段验证是其后追加的结构核对，不重写原冻结。模型阶段仍为第二阶段 15 请求、163,209 已报告 token；本目录消耗 0 模型请求。
