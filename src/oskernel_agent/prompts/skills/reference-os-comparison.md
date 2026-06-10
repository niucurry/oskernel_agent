name: reference-os-comparison
description: 与参考 OS（rCore/xv6/uCore）做函数级相似度对比、填 similarity 字段（仅当 facts.meta.reference_os 非空时需要）
applies_to: subsys, verdict
<!-- body -->
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【技能：与参考 OS 的相似度对比】

**触发条件：** 仅当 `facts.meta.reference_os`（VERDICT）或 user message 的 `reference_os`
（SUBSYS）非空时，才需要本技能；为空则跳过、不要做任何相似度叙述。

━━ VERDICT：填充顶层「相似度分析」卡片（`similarity` 字段）━━

数据来源分两种情况：

- 当 `facts.meta.reference_os` 属于工具支持的 OS（`rcore-tutorial-v3` /
  `rcore-tutorial-v2` / `xv6-riscv` / `ucore`）时，**调一次**
  `compare_with_reference_os(facts.meta.reference_os)`，用其重叠率 / 重叠函数 /
  独有函数填 `similarity`（`overlap_pct` 取重叠率）。
- 若 reference_os 不在上述列表内（工具会拒绝）：**不要调用该工具**，改为基于
  `facts.syscall.ref_*` 与子系统证据**定性**填写 similarity，`overlap_pct` 可省略，
  `borrowed` / `original` 仍尽量给出真实 path:line。

`similarity` 字段契约（写入 outputs.json_path 的 JSON 中，与其它字段并列）：

```json
"similarity": {
  "reference_os": "ucore",
  "overlap_pct": 45,
  "level": "中",
  "summary": "框架层沿用 ucore，调度与文件系统有显著改造（≤200 字）",
  "borrowed": [
    {"path":"kernel/proc.c:42", "quote":"do_fork 流程与 ucore 基本一致"}
  ],
  "original": [
    {"path":"sched/cfs.c:10", "quote":"新增 CFS 调度器，ucore 无此实现"}
  ]
}
```

字段约束：
- `reference_os`：取自 `facts.meta.reference_os`；为空时整个 similarity 可省略。
- `overlap_pct`：integer 0–100，取自 `compare_with_reference_os` 的重叠率。
- `level`：定性结论，仅取 `高` / `中` / `低` 之一。
- `summary`：一句话中文总述，≤200 字。
- `borrowed`：沿用 / 借鉴参考 OS 之处，2–5 项，每项带真实 path:line + 中文说明。
- `original`：改造 / 原创之处，2–5 项，每项带真实 path:line + 中文说明。

注意：相似度只进 `similarity` 字段卡片，**不要**在详细评判正文里另写「原创性 / 相似度分析」
一节，正文重复会与卡片冲突。原创性维度的 `dimensions[].reason` 可引用本对比结论。

━━ SUBSYS：子系统总览中的「与 reference OS 对比」段 ━━

在子系统总览 HTML 片段（outputs.content_path）中追加一节：

```html
<h3>与 reference OS 对比</h3>
<p>{2–4 句：哪些部分对齐参考实现、哪些有改动、哪些是创新（如有），
每条结论尽量附真实 path:line}</p>
```

仅当本子系统的 reference_os 非空时写这一节；为空则整节省略。
