# -*- coding: utf-8 -*-
"""把已生成描述报告里「亮点/槽点」中粘贴的源码摘录改写成中文一句话点评。

subsys.md 要求 quote 是「中文一句话点评（不是粘贴源码原文）」，但 LLM 常直接贴代码/TODO。
本脚本对每个 data/output/<队伍编号>/<队伍编号>_description.tree.json：
  1. lang_guard.normalize_tree_quotes：检测 highlights/issues 里的代码摘录型 quote → LLM 改中文点评
  2. 有改动则回写 tree.json + 用 write_tree_html 重渲染 HTML（repo_roots 指向克隆保链接）
无需重跑 OpenCode 流水线；并发 + 缓存 + 重试；幂等（改成中文后再跑会跳过）。

用法：
    python fix_report_quotes.py                     # 全部
    python fix_report_quotes.py T2026100079910437   # 指定队伍
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from oskernel_agent.pipeline.lang_guard import normalize_tree_quotes  # noqa: E402
from oskernel_agent.reports.html_tree import write_tree_html  # noqa: E402

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "output"
REPOS = OUT / "_repos"


def _fork_name_for(team_id: str) -> str | None:
    try:
        teams = json.loads((ROOT / "作品.txt").read_text(encoding="utf-8"))
    except Exception:
        return None
    for e in teams:
        if e.get("队伍编号") == team_id:
            return e["Fork地址"].rstrip("/").split("/")[-1].removesuffix(".git")
    return None


def fix_one(tree_json: Path) -> str:
    team_id = tree_json.parent.name
    tree = json.loads(tree_json.read_text(encoding="utf-8"))
    stats = normalize_tree_quotes(tree)
    n = stats.get("rewritten", 0)
    if not n:
        return f"skip  {team_id}（无代码摘录型 quote）"
    tree_json.write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path = tree_json.with_name(f"{team_id}_description.html")
    fork = _fork_name_for(team_id)
    repo_roots = [REPOS / fork] if fork and (REPOS / fork).exists() else None
    try:
        write_tree_html(html_path, tree, repo_roots=repo_roots)
    except Exception as e:  # noqa: BLE001
        return f"WARN  {team_id}: 改写 {n} 条但渲染失败：{e}"
    return f"OK    {team_id}: 代码摘录改中文点评 {n} 条并重渲染"


def main() -> None:
    targets = sys.argv[1:]
    tjs = sorted(OUT.glob("T*/*_description.tree.json"))
    if targets:
        tjs = [p for p in tjs if p.parent.name in targets]
    print(f"待处理 tree.json：{len(tjs)} 份", flush=True)
    for tj in tjs:
        print(fix_one(tj), flush=True)


if __name__ == "__main__":
    main()
