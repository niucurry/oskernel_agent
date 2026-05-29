"""
CLI 树状报告打印：把 tree.json 渲染成 rich.tree.Tree。

入口：print_tree(tree_json, max_depth=3, score_filter=None)
末尾打印 verdict 卡片（与 tree 用醒目分隔线分离，强调"评判 vs 中性"）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree
from rich import box

# 颜色阈值
_SCORE_GREEN = 85
_SCORE_YELLOW = 70


def _score_style(score: int) -> str:
    if score >= _SCORE_GREEN:
        return "bold green"
    if score >= _SCORE_YELLOW:
        return "bold yellow"
    return "bold red"


def _score_block(score: int) -> str:
    """评分色块标签。"""
    style = _score_style(score)
    return f"[{style}]█ {score}[/{style}]"


def _node_label(node: dict, is_root: bool = False) -> str:
    typ = node.get("type", "")
    name = node.get("name") or node.get("path") or "/"
    score = int(node.get("score") or 0)
    role = node.get("role", "")
    summary = node.get("summary", "")
    one_line = summary.splitlines()[0] if summary else ""
    if len(one_line) > 80:
        one_line = one_line[:77] + "..."

    label_parts = [_score_block(score)]
    if typ == "subsystem":
        label_parts.append(f"[bold cyan]【{name}】[/bold cyan]")
    elif typ == "module":
        label_parts.append(f"[bold magenta]{name}[/bold magenta]")
    elif typ == "root":
        label_parts.append(f"[bold]{name}[/bold]")
    elif typ == "dir":
        label_parts.append(f"[bold cyan]{name}/[/bold cyan]")
    else:
        label_parts.append(f"[white]{name}[/white]")
    if role:
        label_parts.append(f"[dim]({role})[/dim]")
    if one_line:
        label_parts.append(f"[dim]— {one_line}[/dim]")
    return " ".join(label_parts)


def _add_children(rich_node: Tree, node: dict, depth: int,
                  max_depth: int, score_filter: Callable[[dict], bool] | None) -> None:
    if depth >= max_depth:
        children = node.get("children", [])
        if children:
            rich_node.add(f"[dim italic]... 还有 {len(children)} 个子节点（"
                          f"加大 --depth 查看）[/dim italic]")
        return

    for child in node.get("children", []):
        if score_filter is not None and not score_filter(child):
            continue
        sub = rich_node.add(_node_label(child))
        if child.get("type") == "module":
            # 模块叶子：列出涉及的文件路径
            for fp in (child.get("file_paths") or [])[:5]:
                sub.add(f"[dim]· {fp}[/dim]")
        else:
            _add_children(sub, child, depth + 1, max_depth, score_filter)


def render_tree(tree_json: dict, max_depth: int = 3,
                score_filter: Callable[[dict], bool] | None = None) -> Tree:
    """返回 rich.tree.Tree 对象。"""
    root = tree_json.get("tree", {})
    rich_root = Tree(_node_label(root, is_root=True))
    _add_children(rich_root, root, 1, max_depth, score_filter)
    return rich_root


def render_verdict(tree_json: dict) -> Panel:
    """渲染顶层 verdict 卡片。"""
    verdict = tree_json.get("verdict", {}) or {}
    score_total = int(verdict.get("score_total") or 0)
    one_line = verdict.get("one_line", "")

    # 维度评分表
    dim_table = Table(box=box.SIMPLE_HEAVY, show_header=True,
                      header_style="bold magenta", title="评分维度")
    dim_table.add_column("维度", style="cyan", no_wrap=True)
    dim_table.add_column("得分", justify="center")
    dim_table.add_column("评语")
    for dim in verdict.get("dimensions", []):
        s = int(dim.get("score") or 0)
        dim_table.add_row(
            dim.get("name", ""),
            f"[{_score_style(s)}]{s}[/{_score_style(s)}]",
            dim.get("reason", ""),
        )

    # 亮点 / 槽点 两栏
    hi_table = Table(box=box.SIMPLE, show_header=False, title="亮点")
    hi_table.add_column(style="green")
    for h in verdict.get("highlights", []):
        hi_table.add_row(f"[bold]{h.get('path','?')}[/bold]: {h.get('quote','')}")
    if not verdict.get("highlights"):
        hi_table.add_row("[dim](无)[/dim]")

    is_table = Table(box=box.SIMPLE, show_header=False, title="槽点")
    is_table.add_column(style="red")
    for issue in verdict.get("issues", []):
        sev = issue.get("severity", "")
        is_table.add_row(
            f"[bold]{issue.get('path','?')}[/bold] "
            f"[{_sev_style(sev)}]{sev}[/{_sev_style(sev)}]: {issue.get('quote','')}"
        )
    if not verdict.get("issues"):
        is_table.add_row("[dim](无)[/dim]")

    # 装配
    inner = Table.grid(padding=1)
    inner.add_column()
    inner.add_row(
        f"[{_score_style(score_total)}]总分: {score_total}[/{_score_style(score_total)}]   "
        f"[bold italic]{one_line}[/bold italic]"
    )
    inner.add_row(dim_table)
    cols = Table.grid(padding=2)
    cols.add_column()
    cols.add_column()
    cols.add_row(hi_table, is_table)
    inner.add_row(cols)

    return Panel(inner, title="[bold]顶层评判 verdict[/bold]", border_style="cyan")


def _sev_style(sev: str) -> str:
    return {"low": "yellow", "medium": "orange3", "high": "red"}.get(sev, "white")


def print_tree(tree_json: dict, max_depth: int = 3,
               score_filter: Callable[[dict], bool] | None = None) -> None:
    """主入口：把 verdict + tree 打印到终端。"""
    console = Console()
    meta = tree_json.get("meta", {})
    console.rule(f"[bold]{meta.get('repo','?')}  —  {meta.get('ts','?')}  "
                  f"({meta.get('indexed_files',0)} 个源文件)[/bold]")
    console.print(render_verdict(tree_json))
    console.rule("[bold]代码树（下层 = 中性描述）[/bold]")
    console.print(render_tree(tree_json, max_depth, score_filter))


def parse_score_filter(expr: str | None) -> Callable[[dict], bool] | None:
    """解析 --filter 表达式，如 "score<70"，返回 child→bool 的过滤函数。"""
    if not expr:
        return None
    m = expr.strip().replace(" ", "")
    if m.startswith("score<"):
        try:
            thr = int(m[len("score<"):])
            return lambda node: int(node.get("score") or 0) < thr
        except ValueError:
            return None
    if m.startswith("score>="):
        try:
            thr = int(m[len("score>="):])
            return lambda node: int(node.get("score") or 0) >= thr
        except ValueError:
            return None
    return None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="打印 tree.json")
    ap.add_argument("tree_json")
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--filter", default=None,
                    help="过滤子节点，如 score<70")
    args = ap.parse_args()
    data = json.loads(Path(args.tree_json).read_text(encoding="utf-8"))
    print_tree(data, max_depth=args.depth,
                score_filter=parse_score_filter(args.filter))
