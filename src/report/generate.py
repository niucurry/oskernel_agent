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


async def _generate_async(suspects, recall, client) -> tuple[str, int]:
    top_repos = S.trace_top_repos(suspects)
    mod_rows = S.module_table_rows(suspects)
    hi_pairs = S.high_similarity_pairs(suspects)
    innov = S.innovation_functions(recall)
    ann = S.annotations(suspects)
    allowed = _collect_allowed(suspects, innov)

    # 并发取 LLM 文本
    s1_task = _complete(client, P.SECTION1_SYSTEM, P.section1_user(top_repos)) if top_repos else _noop()
    s3_task = (
        _complete(client, P.SECTION3_SYSTEM, P.section3_user([p["reasoning"] for p in hi_pairs]))
        if (hi_pairs and client) else _noop()
    )
    s4_task = _complete(client, P.SECTION4_SYSTEM, P.section4_user(innov)) if (innov and client) else _noop()
    s1_text, s3_raw, s4_text = await asyncio.gather(s1_task, s3_task, s4_task)

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

    # 组装
    md = [
        "# 作品查重评审报告", "",
        "## 一、溯源结论", "", s1_text, "",
        "## 二、模块级对照表", "", S.render_module_table(mod_rows), "",
        "## 三、高相似代码段清单", "",
        (S.render_high_sim_table(hi_pairs, summaries) if hi_pairs else "（无 confirmed / likely_clone 对）"), "",
        "## 四、创新点分析", "", s4_text, "",
        "## 五、附注信号", "", S.render_annotations(ann), "",
    ]
    if deleted:
        md.append(f"\n---\n> 后置校验：已删除 {deleted} 条无法回源（文件:行号 越界）的陈述。")
    return "\n".join(md), deleted


async def _noop():
    return ""


def generate_report(reviewed_data: dict, recall_data: dict, client=None) -> tuple[str, int]:
    """返回 (report_markdown, deleted_count)。client 为 None 时不调用 LLM（模板兜底）。"""
    suspects = reviewed_data.get("suspects", [])
    return asyncio.run(_generate_async(suspects, recall_data, client))


def run_report(
    reviewed_path: str | Path,
    recall_path: str | Path,
    *,
    client=None,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    repo_name: str | None = None,
) -> dict:
    reviewed = json.loads(Path(reviewed_path).read_text(encoding="utf-8"))
    recall = json.loads(Path(recall_path).read_text(encoding="utf-8"))
    md, deleted = generate_report(reviewed, recall, client)

    name = repo_name or (recall.get("query_repo_id") or "report").replace("/", "_")
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{name}_report.md"
    out_path.write_text(md, encoding="utf-8")
    logger.info("报告写入 {}（删除无法回源陈述 {} 条）", out_path, deleted)
    return {"output_path": str(out_path), "deleted": deleted, "report": md}
