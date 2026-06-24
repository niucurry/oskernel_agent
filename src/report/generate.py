"""报告生成：模板 + LLM 填空（每章节只喂相关结构化数据）+ 后置校验。"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from loguru import logger

from . import prompts as P
from . import sections as S
from .postcheck import add_allowed, scrub

DEFAULT_OUTPUT_DIR = "data/output"


def _collect_allowed(suspects: list[dict], innov: list[dict]) -> dict[str, list[tuple[int, int]]]:
    """收集所有真实 文件:行号 区间作为回源白名单。"""
    allowed: dict[str, list[tuple[int, int]]] = {}
    for s in suspects:
        q, c = s["query_func"], s["candidate_func"]
        add_allowed(allowed, f"{q['file_path']}:{q['start_line']}-{q['end_line']}")
        add_allowed(allowed, f"{c['repo_id']}/{c['file_path']}:{c['start_line']}-{c['end_line']}")
    for f in innov:
        add_allowed(allowed, f["ref"])
    return allowed


async def _complete(client, system: str, user: str) -> str:
    if client is None:
        return ""
    return await client.complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user}], 0.2
    )


def _sanitize_cell(text: str, limit: int = 50) -> str:
    text = re.sub(r"\s+", " ", text or "").replace("|", "/").strip()
    return text[:limit]


async def _generate_async(suspects, recall, client, ai_report=None) -> tuple[str, int]:
    top_repos = S.trace_top_repos(suspects)
    mod_rows = S.module_table_rows(suspects)
    hi_pairs = S.high_similarity_pairs(suspects)
    innov = S.innovation_functions(recall)
    ann = S.annotations(suspects)
    allowed = _collect_allowed(suspects, innov)

    # 章六：AI 生成代码检测（与其他模块一样唤起独立会话生成结论）
    ai_data = S.ai_detection_data(ai_report)
    if ai_data.get("status") == "ok":
        for s in ai_data.get("suspicious", []):
            add_allowed(allowed, s["ref"])  # 登记可疑函数 文件:行 供后置回源校验

    # 并发取 LLM 文本
    s1_task = _complete(client, P.SECTION1_SYSTEM, P.section1_user(top_repos)) if top_repos else _noop()
    s3_task = (
        _complete(client, P.SECTION3_SYSTEM, P.section3_user([p["reasoning"] for p in hi_pairs]))
        if (hi_pairs and client) else _noop()
    )
    s4_task = _complete(client, P.SECTION4_SYSTEM, P.section4_user(innov)) if (innov and client) else _noop()
    s6_task = (
        _complete(client, P.SECTION6_SYSTEM, P.section6_user(ai_data))
        if (ai_data.get("status") == "ok" and client) else _noop()
    )
    s1_text, s3_raw, s4_text, s6_text = await asyncio.gather(s1_task, s3_task, s4_task, s6_task)

    deleted = 0
    # 章一：溯源结论（LLM 散文 → 后置校验）
    if not s1_text:
        s1_text = "（未启用 LLM）按加权命中排名，最相似历史作品见下表。\n" + "\n".join(
            f"- {r['repo_id']}：嫌疑对 {r['pairs']}（confirmed {r['confirmed']} / review {r['review']}），涉及模块 {', '.join(r['modules'])}"
            for r in top_repos
        )
    else:
        s1_text, d = scrub(s1_text, allowed); deleted += d

    # 章三摘要
    summaries = []
    if hi_pairs:
        parsed = None
        if s3_raw:
            try:
                parsed = json.loads(s3_raw[s3_raw.find("["): s3_raw.rfind("]") + 1])
            except (json.JSONDecodeError, ValueError):
                parsed = None
        for i, p in enumerate(hi_pairs):
            summ = parsed[i] if (parsed and i < len(parsed)) else p["reasoning"]
            summaries.append(_sanitize_cell(summ))

    # 章四：创新点（LLM 散文 → 后置校验）
    if not innov:
        s4_text = "未发现与历史库相似度 < 0.5 且行数 > 30 的函数（该作品与历史高度重合，独立实现部分少）。"
    elif not s4_text:
        s4_text = "（未启用 LLM）以下函数与历史库相似度低、规模较大，疑为独立实现：\n" + "\n".join(
            f"- {f['func_name']} ({f['ref']})，{f['lines']} 行，最高相似度 {f['max_sim']}" for f in innov
        )
    else:
        s4_text, d = scrub(s4_text, allowed); deleted += d

    # 章六：AI 生成代码检测正文（散文走 LLM → 后置校验；表格纯代码生成）
    s6_body, s6_extra = _ai_section_body(ai_data, s6_text, allowed)
    deleted += s6_extra

    # 组装
    md = [
        "# 作品查重评审报告", "",
        "## 一、溯源结论", "", s1_text, "",
        "## 二、模块级对照表", "", S.render_module_table(mod_rows), "",
        "## 三、高相似代码段清单", "",
        (S.render_high_sim_table(hi_pairs, summaries) if hi_pairs else "（无 confirmed / likely_clone 对）"), "",
        "## 四、创新点分析", "", s4_text, "",
        "## 五、附注信号", "", S.render_annotations(ann), "",
        "## 六、AI 生成代码检测", "", s6_body, "",
    ]
    if deleted:
        md.append(f"\n---\n> 后置校验：已删除 {deleted} 条无法回源（文件:行号 越界）的陈述。")
    return "\n".join(md), deleted


async def _noop():
    return ""


def _ai_section_body(ai_data: dict, s6_text: str, allowed: dict) -> tuple[str, int]:
    """拼装章六正文：状态分支 + LLM 散文（回源校验）+ 代码生成表格。返回 (markdown, 删除句数)。"""
    status = ai_data.get("status")
    if status == "missing":
        return ("未运行 AI 生成代码检测。可在带 GPU 的环境单独执行 "
                "`python -m src.ai_detect --repo <作品路径>` 生成 `{repo}_ai_detect.json` 后并入本报告。"), 0
    if status != "ok":
        reason = ai_data.get("reason", "")
        return f"AI 生成代码检测未完成（status={status}）：{reason}", 0

    deleted = 0
    overall = ai_data.get("overall", {})
    if not s6_text:
        llm = overall.get("llm_count", 0)
        total = overall.get("total_functions", 0)
        s6_text = (f"（未启用 LLM）检测模型 {ai_data.get('model_id', '')}：共分析 {total} 个函数，"
                   f"其中 AI 疑似 {llm} 个。详见下表。结论为概率性信号，仅供人工复核参考。")
    else:
        s6_text, deleted = scrub(s6_text, allowed)

    parts = [
        s6_text, "",
        S.render_ai_overview_table(overall), "",
        "**分语言：**", "", S.render_ai_language_table(ai_data.get("by_language", [])), "",
        "**高风险文件：**", "", S.render_ai_highrisk_table(ai_data.get("high_risk_files", [])), "",
        "**高置信 AI 疑似函数：**", "", S.render_ai_suspicious_table(ai_data.get("suspicious", [])),
    ]
    author_tbl = S.render_ai_author_table(ai_data.get("by_author", []))
    if author_tbl:
        parts.append(author_tbl)
    parts += ["", "> 检测方法：DetectCodeGPT（困惑度/log-rank，免训练）。结果为概率性信号，非定论；"
              "短函数与样板代码、Python/JS 等语言易误报，请结合人工复核。"]
    return "\n".join(parts), deleted


def generate_report(reviewed_data: dict, recall_data: dict, client=None, ai_report: dict | None = None) -> tuple[str, int]:
    """返回 (report_markdown, deleted_count)。client 为 None 时不调用 LLM（模板兜底）。

    ai_report：{repo}_ai_detect.json 内容（AI 生成代码检测结果），为 None 时章六给出未运行说明。
    """
    suspects = reviewed_data.get("suspects", [])
    return asyncio.run(_generate_async(suspects, recall_data, client, ai_report))


def run_report(
    reviewed_path: str | Path,
    recall_path: str | Path,
    *,
    client=None,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    repo_name: str | None = None,
    ai_detect_path: str | Path | None = None,
) -> dict:
    reviewed = json.loads(Path(reviewed_path).read_text(encoding="utf-8"))
    recall = json.loads(Path(recall_path).read_text(encoding="utf-8"))
    ai_report = None
    if ai_detect_path and Path(ai_detect_path).exists():
        ai_report = json.loads(Path(ai_detect_path).read_text(encoding="utf-8"))
    md, deleted = generate_report(reviewed, recall, client, ai_report)

    name = repo_name or (recall.get("query_repo_id") or "report").replace("/", "_")
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{name}_report.md"
    out_path.write_text(md, encoding="utf-8")
    logger.info("报告写入 {}（删除无法回源陈述 {} 条）", out_path, deleted)
    return {"output_path": str(out_path), "deleted": deleted, "report": md}
