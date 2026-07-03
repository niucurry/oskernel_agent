# -*- coding: utf-8 -*-
"""把已生成的描述报告里的英文正文中文化——无需重跑昂贵的 OpenCode 流水线。

对每个 data/output/<队伍编号>/<队伍编号>_description.tree.json：
  1. 读回 tree.json
  2. lang_guard.normalize_tree_language（英文正文 → 中文，缓存去重）
  3. 若有改动：回写 tree.json + 用 write_tree_html 重新渲染 HTML
     （repo_roots 指向 data/output/_repos/<fork名>，保证 file:line 链接仍可用）

用法：
    python scripts/fix_report_language.py                 # 处理全部
    python scripts/fix_report_language.py T2026101269910207 T2026100069910965   # 指定队伍
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oskernel_agent.pipeline.lang_guard import (  # noqa: E402
    normalize_tree_language, normalize_tree_titles)
from oskernel_agent.reports.html_tree import write_tree_html  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "output"
REPOS = OUT / "_repos"


def _fork_name_for(team_id: str) -> str | None:
    """从 作品.txt 里查该队伍的 fork 目录名（<队伍编号>-NNN）。"""
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
    stats = normalize_tree_language(tree)
    tstats = normalize_tree_titles(tree)
    n = stats.get("translated", 0) + tstats.get("translated", 0)
    if not n:
        return f"skip  {team_id}（无英文正文/标题）"

    # 回写 tree.json
    tree_json.write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")

    # 重新渲染 HTML
    html_path = tree_json.with_name(f"{team_id}_description.html")
    fork = _fork_name_for(team_id)
    repo_roots = None
    if fork and (REPOS / fork).exists():
        repo_roots = [REPOS / fork]
    try:
        write_tree_html(html_path, tree, repo_roots=repo_roots)
    except Exception as e:  # noqa: BLE001
        return f"WARN  {team_id}: 翻译 {n} 字段但渲染失败：{e}"
    return f"OK    {team_id}: 中文化 {n} 字段并重渲染 → {html_path.name}"


def main() -> None:
    targets = sys.argv[1:]
    tjs = sorted(OUT.glob("T*/*_description.tree.json"))
    if targets:
        tjs = [p for p in tjs if p.parent.name in targets]
    print(f"待处理 tree.json：{len(tjs)} 份")
    for tj in tjs:
        print(fix_one(tj), flush=True)


if __name__ == "__main__":
    main()
