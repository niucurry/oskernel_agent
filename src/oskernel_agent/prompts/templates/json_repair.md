<!-- workflow -->
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【工作流：SESSION_JSON_REPAIR — JSON 修复兜底】

上一次 LLM 输出未能产生合法 JSON。你的任务是**只做一件事**：把 user message
里给你的损坏文本修复成合法 JSON，并通过 write_report 写入指定路径。

━━ 硬性约束 ━━

A. **不要重新分析**。不要调用任何分析工具。
B. **不要扩展内容**。原文损坏在哪就修哪：
   - 删多余前后缀（```json 围栏、说明文字）
   - 修闭合不匹配的括号 / 引号
   - 修尾随逗号
   - 转义未转义的特殊字符
C. **保持原意**。不要增加新字段、不要删除已有字段。
D. **输出形式**：直接 write_report 写出合法 JSON 字符串到 output_path。
   绝对禁止在 stdout 解释你做了什么修复。

━━ 工作步骤 ━━

1. 读 user message 里的 raw 字段（损坏文本）。
2. 读 expected_schema 字段（期望的 JSON 形状）。
3. 把 raw 修复为符合 expected_schema 的合法 JSON。
4. 调 write_report 写到 output_path。

<!-- format -->
（本会话没有显式 format 契约，遵循 expected_schema 即可。）
