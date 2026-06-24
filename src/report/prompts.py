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


SECTION6_SYSTEM = """你是 AI 生成代码检测报告撰写助手。检测方法是 DetectCodeGPT（基于参考模型的
困惑度/log-rank，免训练），结论是**概率性信号而非定论**。给你这份新作品的检测统计 JSON（总体
比例、分语言、高风险文件、若干高置信「AI 疑似」函数及其代码与 文件:行号）。请用简洁中文写一段
「AI 生成代码检测」结论，包含：①整体 AI 疑似程度（点名数字与比例）；②最可疑的文件/函数（**每条
具体结论都要带给定的 文件:行号 引用，直接用 ref，不要改数字**）；③必要的局限性说明（Python/JS
精度偏低、短函数与样板代码易误报，仅供人工复核参考）。禁止编造数据中没有的文件、函数或数字。
只输出正文段落，不要标题。若统计显示几乎没有 AI 疑似函数，请直接说明该作品整体未见明显 AI 生成特征。"""


def section6_user(data: dict) -> str:
    import json
    overall = data.get("overall", {})
    parts = [
        "检测模型：" + str(data.get("model_id", "")),
        "总体统计（JSON）：\n" + json.dumps(overall, ensure_ascii=False),
        "分语言（JSON）：\n" + json.dumps(data.get("by_language", []), ensure_ascii=False),
        "高风险文件（JSON）：\n" + json.dumps(data.get("high_risk_files", []), ensure_ascii=False),
    ]
    if data.get("suspicious"):
        parts.append("高置信 AI 疑似函数（含代码）：")
        for s in data["suspicious"]:
            parts.append(
                f"### {s['name']}  (ref: {s['ref']}, 置信度 {s['confidence']}, 语言 {s['language']})\n"
                f"```\n{s['source']}\n```"
            )
    return "\n\n".join(parts)


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
