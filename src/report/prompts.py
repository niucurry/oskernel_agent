"""报告生成提示词（模板 + 填空，每章节只喂该章节的结构化数据，控制幻觉）。"""

from __future__ import annotations

SECTION1_SYSTEM = """你是代码查重报告撰写助手。只依据给定的 JSON 统计数据撰写「溯源结论」段落，
用简洁中文说明这份新作品最可能借鉴/抄袭了哪几个历史作品。必须点名 repo_id，给出嫌疑对数量与
confirmed/review 分布。禁止编造数据中没有的仓库、数字或文件。只输出正文段落，不要标题。"""

SECTION3_SYSTEM = """你是代码查重摘要助手。给你一个 JSON 数组，每项是一条「判定理由」原文。
请把每条压缩到 50 个汉字以内的要点，保留关键技术依据，不要换行、不要使用竖线 |。
严格输出与输入等长的 JSON 字符串数组，不要任何额外文字。"""

SECTION4_SYSTEM = """你是代码评审助手。给你新作品中若干「与历史库相似度很低」的函数（含代码与
文件:行号）。请客观描述这些函数体现的「该作品的独立实现部分」，逐条说明其做了什么。
**每一条都必须带上对应的 文件:行号 引用**（直接用给定的 ref，不要改数字）。只依据给定代码，
不要假设未给出的内容。只输出要点列表。"""

PROFILE_SYSTEM = """你是 OS 内核作品分析助手。依据给定的「模块分布统计 + 各模块代表函数代码（含
文件:行号）+ README 摘录」，为该历史作品写一份简洁档案，包含：整体架构、各子系统实现方式、
特色点。**每条具体陈述都要带 文件:行号 引用**（用给定的 ref，不要改数字）。只依据给定材料，
不要编造。输出 Markdown 要点。"""


def section1_user(top_repos: list[dict]) -> str:
    import json
    return "历史来源统计（JSON）：\n" + json.dumps(top_repos, ensure_ascii=False, indent=2)


def section3_user(reasonings: list[str]) -> str:
    import json
    return json.dumps(reasonings, ensure_ascii=False)


def section4_user(funcs: list[dict]) -> str:
    blocks = []
    for f in funcs:
        blocks.append(f"### {f['func_name']}  (ref: {f['ref']}, 与历史库最高相似度 {f['max_sim']})\n```\n{f['raw_code']}\n```")
    return "\n\n".join(blocks)


def profile_user(repo_id: str, dist: dict, reps: list[dict], readme: str) -> str:
    import json
    parts = [f"# 仓库 {repo_id}", "模块分布：" + json.dumps(dist, ensure_ascii=False)]
    parts.append("\n代表函数：")
    for r in reps:
        parts.append(f"### {r['module_tag']} :: {r['func_name']}  (ref: {r['ref']})\n```\n{r['raw_code']}\n```")
    if readme:
        parts.append("\nREADME 摘录：\n" + readme[:1500])
    return "\n".join(parts)
