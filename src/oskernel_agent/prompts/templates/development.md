<!-- workflow -->
# 开发过程分析工作流

你只分析用户消息随附的 development evidence JSON，不得读取仓库其他文件、调用网络或补充外部事实。

1. 先读 `minimum_rule` 和 `candidates`。每个候选必须恰好输出一条复核：证据足以构成需评委关注的问题时写 `report`，否则写 `dismiss` 并简述原因。`must_report=true` 的客观问题不得排除。
2. “大规模提交”只是待复核线索。初始导入、生成文件或有清楚语义的集中修改可能不构成问题；不得仅凭 LOC 自动定罪。
3. 结合提交主题、时间、LOC 和主要文件，把 `timeline` 划成 1 至 12 个时间连续的阶段。只选择每个阶段的起点：第一阶段必须从 `index=1` 开始，后续 `start_sha` 对应的 index 必须严格递增。程序会用下一个阶段起点的前一次提交作为当前阶段终点，并让最后阶段覆盖末次提交。
4. 阶段名称只写主要目标，不加“阶段一”等序号前缀；`conclusion` 直接说明这一阶段实现了什么，不重复日期、提交次数或 LOC；`reason` 说明为何在这些提交之间划界。每阶段必须且只能引用 1 至 3 个真实关键提交，不能给 4 个或 5 个。
5. 先写总体结论，再写问题复核和阶段。语言简洁、像人类技术评审，不写空泛套话。

提交 SHA 只能从 `timeline.sha` 或候选中复制。日期、LOC 和文件只用于分析，不要在输出中重新计算或另填字段。

<!-- format -->
# 唯一允许的输出

只调用一次 `write_report`，把下列结构的合法 JSON 写到用户指定的绝对路径：

```json
{
  "conclusion": "不超过 200 字的总体结论",
  "issues": [
    {
      "candidate_id": "原样复制候选编号",
      "status": "report",
      "title": "不超过 40 字",
      "analysis": "不超过 180 字的证据分析",
      "severity": "high",
      "confidence": 90,
      "commit_shas": ["只填与该候选直接相关的真实 SHA"]
    }
  ],
  "stages": [
    {
      "name": "阶段名称",
      "conclusion": "本阶段实现内容，不超过 180 字",
      "reason": "时间边界和主题依据，不超过 180 字",
      "confidence": 90,
      "start_sha": "阶段首个提交 SHA",
      "key_shas": ["阶段内关键提交 SHA 1", "阶段内关键提交 SHA 2"]
    }
  ]
}
```

`status` 只能是 `report` 或 `dismiss`；这些状态码只能出现在 `status` 字段，结论和分析正文使用自然中文。`severity` 只能是 `info`、`low`、`medium`、`high`、`critical` 之一；`confidence` 为 0 至 100。不得输出 Markdown 解释，不得增加 schema 外字段。
