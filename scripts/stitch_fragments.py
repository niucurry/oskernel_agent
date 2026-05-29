#!/usr/bin/env python3
"""把已生成的分片（*_tree_work 目录）拼接回 tree.json 并渲染 HTML。

用途：当 SUBSYS / VERDICT 阶段的分片已落盘，但还没合成最终报告时，
用现有代码的装配口径离线拼接，无需重跑 LLM。

用法：
    python3 scripts/stitch_fragments.py <work_dir> [--repo-path <仓库根>]
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from oskernel_agent.pipeline.tree_builder import (  # noqa: E402
    MAX_MODULES_PER_SUBSYS,
    SCHEMA_VERSION,
    _SUBSYS_DISPLAY_ORDER,
    _clean_tree,
    _read_md_if_exists,
    _safe_filename_part,
    write_tree_json,
)
from oskernel_agent.reports.html_tree import write_tree_html  # noqa: E402


def _build_subsys_node(name: str, work_dir: Path) -> dict | None:
    """从 subsys-<name>.json + .content.md + .module-NNN.md 重建子系统节点。"""
    safe = _safe_filename_part(name)
    base = f"subsys-{safe}"
    json_path = work_dir / f"{base}.json"
    if not json_path.exists():
        return None

    parsed = json.loads(json_path.read_text(encoding="utf-8"))
    node: dict = {
        "type":       "subsystem",
        "name":       parsed.get("name", name),
        "path":       f"<subsys>/{name}",
        "role":       parsed.get("role", name),
        "summary":    parsed.get("summary", ""),
        "score":      int(parsed.get("score", 60)),
        "content":    _read_md_if_exists(work_dir / f"{base}.content.md"),
        "highlights": parsed.get("highlights", []),
        "issues":     parsed.get("issues", []),
        "children":   [],
    }

    for i, m in enumerate(parsed.get("modules") or [], start=1):
        slot = int(m.get("slot") or i)
        if not (1 <= slot <= MAX_MODULES_PER_SUBSYS):
            continue
        mod_md = work_dir / f"{base}.module-{slot:03d}.md"
        node["children"].append({
            "type":       "module",
            "name":       m.get("name", f"模块 {slot}"),
            "path":       f"{node['path']}/m{slot:03d}",
            "summary":    m.get("summary", ""),
            "score":      int(m.get("score", 60)),
            "file_paths": m.get("file_paths", []),
            "content":    _read_md_if_exists(mod_md),
        })
    return node


def stitch(work_dir: Path, repo_path: Path | None) -> dict:
    # work_dir 形如 <repo>_<ts>_tree_work
    stem = work_dir.name
    if stem.endswith("_tree_work"):
        stem = stem[: -len("_tree_work")]
    # 拆出 repo 与 ts（ts 形如 YYYYMMDD_HHMMSS，取最后两段）
    parts = stem.rsplit("_", 2)
    if len(parts) == 3:
        repo_name = parts[0]
        ts = f"{parts[1]}_{parts[2]}"
    else:
        repo_name, ts = stem, ""

    root: dict = {
        "type": "root", "name": repo_name, "path": "", "children": [],
    }
    # 先按显示顺序，再补上顺序之外出现的子系统
    seen: set[str] = set()
    ordered = list(_SUBSYS_DISPLAY_ORDER)
    for jp in sorted(work_dir.glob("subsys-*.json")):
        nm = jp.stem[len("subsys-"):]
        if nm not in ordered:
            ordered.append(nm)

    file_count = 0
    for name in ordered:
        node = _build_subsys_node(name, work_dir)
        if node is None or name in seen:
            continue
        seen.add(name)
        root["children"].append(node)
        for child in node["children"]:
            file_count += len(child.get("file_paths") or [])

    verdict: dict = {}
    vjson = work_dir / "verdict.json"
    if vjson.exists():
        verdict = json.loads(vjson.read_text(encoding="utf-8"))

    return {
        "meta": {
            "repo":           repo_name,
            "ts":             ts,
            "indexed_files":  file_count,
            "schema_version": SCHEMA_VERSION,
        },
        "facts":   {},
        "verdict": verdict,
        "tree":    _clean_tree(root),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("work_dir")
    ap.add_argument("--repo-path", default=None)
    args = ap.parse_args()

    work_dir = Path(args.work_dir).resolve()
    repo_path = Path(args.repo_path).resolve() if args.repo_path else None

    tree = stitch(work_dir, repo_path)

    out_base = work_dir.parent / work_dir.name.replace("_tree_work", "")
    tree_json_path = out_base.with_suffix(".tree.json")
    html_path = out_base.with_suffix(".html")

    write_tree_json(tree, tree_json_path)
    print(f"[stitch] tree.json → {tree_json_path}")

    _, broken = write_tree_html(
        html_path, tree,
        repo_roots=[repo_path] if repo_path else None,
    )
    print(f"[stitch] HTML → {html_path}")
    n_sub = len(tree["tree"]["children"])
    n_mod = sum(len(c["children"]) for c in tree["tree"]["children"])
    print(f"[stitch] 子系统 {n_sub} 个 / 模块 {n_mod} 个；断链 {len(broken)} 个")


if __name__ == "__main__":
    main()
