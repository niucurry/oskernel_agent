"""
OS 内核代码分析智能体 — 自底向上树状报告管道

流程：
  1. 解析 CLI 参数、克隆远程仓库（必要时）
  2. 采集仓库共享事实档案（analysis/repo_facts.py）
  3. 调 tree_builder.build_tree() 自底向上产出 tree.json
  4. 终端打印（rich.tree） + HTML 渲染（reports/html_tree）

注意：本管道**不调用 MCP 工具**。所有事实（路径、符号、源码片段）在 user message
里直接注入到 DIR / VERDICT 两个会话。MCP server 由 oskernel-setup 注册，
DIR / VERDICT agent 可按需调用工具（read_file / find_symbol_definition 等）。
"""

import os
import sys
from datetime import datetime
from pathlib import Path

from .. import config
from ..engines.base import AnalysisEngine
from ..parsers.code_parser import (
    find_source_roots,
    classify_files_by_content,
    detect_naming_style,
    find_doc_files,
    detect_anomalies,
)


# 引擎选择：路径 A、B、C 依次降级（保留给 MCP server 的 initialize_analysis 用）

def select_engine(repo_path: str, profile: dict, level2_index) -> AnalysisEngine:
    primary_lang = profile["primary_lang"]
    ecfg = config.engine

    if primary_lang == "rust" or profile.get("has_cargo"):
        from ..engines.path_a import RustAnalyzerEngine
        engine = RustAnalyzerEngine(repo_path, level2_index,
                                    timeout=ecfg["rust_analyzer_timeout"])
        if engine.initialize():
            print("[引擎选择] 路径 A：rust-analyzer")
            return engine

    if primary_lang == "c":
        from ..engines.path_b import ClangdEngine, try_generate_compile_commands
        cc_path = try_generate_compile_commands(repo_path)
        if cc_path:
            engine = ClangdEngine(repo_path, cc_path, level2_index,
                                  timeout=ecfg["clangd_timeout"])
            if engine.initialize():
                print("[引擎选择] 路径 B：clangd")
                return engine

    from ..engines.path_c import TreeSitterEngine
    lang = primary_lang if primary_lang in ("c", "rust") else "c"
    print(f"[引擎选择] 路径 C：tree-sitter（{lang}）")
    return TreeSitterEngine(repo_path, lang, skip_dirs=ecfg["skip_dirs"])


# 静态分析辅助函数（供 mcp_server.py 共用）

def _build_structure(repo_path: Path) -> dict:
    source_roots_rel = find_source_roots(repo_path)
    source_roots_abs = [str(repo_path / r) for r in source_roots_rel]
    all_depths = [
        len(f.relative_to(repo_path).parts)
        for ext in ("*.c", "*.rs")
        for f in repo_path.rglob(ext)
    ]
    avg_depth  = sum(all_depths) / len(all_depths) if all_depths else 0.0
    depth_label = "flat" if avg_depth <= 2 else ("shallow" if avg_depth <= 4 else "deep")
    structure = {
        "doc_files":           find_doc_files(repo_path),
        "subsystem_locations": classify_files_by_content(str(repo_path), source_roots_rel),
        "structure_depth":     depth_label,
        "avg_depth":           round(avg_depth, 1),
        "source_roots":        source_roots_abs,
        "source_roots_rel":    source_roots_rel,
        "naming_style":        detect_naming_style(repo_path),
    }
    structure["anomalies"] = detect_anomalies(structure, repo_path)
    return structure


def _resolve_repo_path(url: str | None, repo_path_arg: str | None,
                       repo_id: str | None) -> tuple[Path, str]:
    if url:
        from .fetch_repo import fetch_repo
        local = fetch_repo(url, output_dir=config.data["repos_dir"])
        p = Path(local).resolve()
        return p, p.name
    if repo_path_arg:
        p = Path(repo_path_arg).resolve()
        return p, p.name
    name = repo_id or config.target.get("repo_id", "")
    if not name:
        print(
            "[错误] 未指定仓库。请使用以下任意一种方式：\n"
            "  --repo-id REPO_NAME       分析 data/historical_repos/ 下的已克隆仓库\n"
            "  --repo-path /path/to/repo 分析本地任意路径\n"
            "  --url https://...         克隆远程仓库后分析\n"
            "或在 config.toml 的 [target] 下填写 repo_id = \"REPO_NAME\"",
            file=sys.stderr,
        )
        sys.exit(1)
    p = (Path(config.data["repos_dir"]) / name).resolve()
    return p, name


def _env_disabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _cleanup_tree_intermediates(output_file: str) -> None:
    """删除描述报告的结构化摘要、树和模型工作目录，仅保留 HTML。"""
    from oskernel_agent.finals.cleanup import remove_directory

    out_html = Path(output_file)
    if out_html.suffix.lower() != ".html":
        out_html = out_html.with_suffix(".html")
    out_html.with_suffix(".tree.json").unlink(missing_ok=True)
    out_html.with_suffix(".digest.json").unlink(missing_ok=True)
    remove_directory(out_html.parent / f"{out_html.stem}_tree_work")


# 树状报告主入口

def _run_tree_mode(repo_path: Path, repo_name: str, output_file: str,
                   cli_depth: int = 3, *, build_log: str | None = None,
                   run_log: str | None = None,
                   team_id: str | None = None,
                   repository_url: str | None = None,
                   repository_ref: str | None = None) -> Path | None:
    """自底向上构建 tree.json，并产出终端打印 + HTML 报告。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 1. 采集共享事实档案
    if _env_disabled("AGENT_NO_FACTS"):
        print("[错误] 决赛作品描述报告必须采集完整事实与硬编码线索，不能禁用 facts", file=sys.stderr)
        return None
    try:
        from ..analysis.repo_facts import build_repo_facts
        print("\n[预处理] 采集项目级共享事实档案 ...")
        facts = build_repo_facts(
            repo_path, repo_name, ts, build_log=build_log, run_log=run_log,
        )
    except Exception as e:
        print(f"[错误] 事实档案构建失败：{e}", file=sys.stderr)
        return None
    if not isinstance(facts.get("integrity"), dict):
        print("[错误] 事实档案缺少编译、运行与硬编码检查结果", file=sys.stderr)
        return None

    # 2. 自底向上构建 tree
    from ..pipeline.tree_builder import build_tree, write_tree_json

    out_html = Path(output_file)
    if out_html.suffix.lower() != ".html":
        out_html = out_html.with_suffix(".html")
    out_html.parent.mkdir(parents=True, exist_ok=True)
    work_dir = out_html.parent / f"{out_html.stem}_tree_work"

    tree = build_tree(repo_path, repo_name, ts, facts=facts,
                       output_dir=work_dir)
    if team_id:
        tree.setdefault("meta", {})["team_id"] = team_id
        # Collision-safe clone names are private workspace keys, not report ids.
        tree.setdefault("meta", {})["repo"] = team_id
    if repository_url:
        tree.setdefault("meta", {})["repository_url"] = repository_url.removesuffix(".git")
    if repository_ref:
        tree.setdefault("meta", {})["repository_ref"] = repository_ref

    # 3. 写 tree.json（单一真相源）
    tree_json_path = out_html.with_suffix(".tree.json")
    write_tree_json(tree, tree_json_path)
    print(f"[tree] tree.json → {tree_json_path}")

    # 决赛四件套共享的短摘要；后续摘要 PDF 直接消费该结构化产物，
    # 不再从冗长 HTML 反向猜测结论。
    try:
        from oskernel_agent.finals.digests import description_digest_from_tree, write_digest
        digest_path = out_html.with_suffix(".digest.json")
        write_digest(digest_path, description_digest_from_tree(tree))
        print(f"[tree] 决赛摘要数据 → {digest_path}")
    except Exception as e:
        print(f"[错误] 决赛摘要数据生成失败：{e}", file=sys.stderr)
        return None

    # 4. 终端打印
    try:
        from .tree_renderer import print_tree
        print_tree(tree, max_depth=cli_depth)
    except Exception as e:
        print(f"[警告] CLI 渲染失败：{e}（继续）", file=sys.stderr)

    # 5. HTML 渲染
    try:
        from ..reports.html_tree import write_tree_html
        html_path, broken = write_tree_html(
            out_html, tree, repo_roots=[repo_path],
        )
        print(f"\n[完成] HTML 报告：{html_path}")
        if broken:
            print(f"[警告] HTML 中有 {len(broken)} 个文件引用断链",
                  file=sys.stderr)
        return html_path
    except Exception as e:
        print(f"[错误] HTML 渲染失败：{e}", file=sys.stderr)
        return None


# 主入口

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="OS 内核仓库自底向上树状报告生成器",
        add_help=True,
    )

    src = parser.add_mutually_exclusive_group()
    src.add_argument("--repo-id")
    src.add_argument("--repo-path")
    src.add_argument("--url")

    parser.add_argument("--output", "-o")
    parser.add_argument("--depth", type=int, default=3,
                        help="终端树打印的最大下钻层数（默认 3）")
    parser.add_argument("--build-log", help="可选：正式编译日志，用于问题前置与证据提取")
    parser.add_argument("--run-log", help="可选：正式运行日志，用于问题前置与证据提取")
    parser.add_argument("--team-id", help="队伍编号，写入报告元数据")
    parser.add_argument("--repository-url", help="目标仓库网页地址；证据链接只使用该地址")
    parser.add_argument("--repository-ref", help="证据链接使用的不可变提交或分支")
    parser.add_argument("--keep-intermediates", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args()

    repo_path_a, repo_name_a = _resolve_repo_path(args.url, args.repo_path, args.repo_id)

    if args.output:
        output_file = str(Path(args.output).resolve())
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        reports_dir = Path(config.data.get("reports_dir", "./data/output")).resolve()
        reports_dir.mkdir(parents=True, exist_ok=True)
        output_file = str(reports_dir / f"{repo_name_a}_{ts}.html")

    print(f"[agent] 报告将写入：{output_file}")
    try:
        result = _run_tree_mode(repo_path_a, repo_name_a, output_file,
                                cli_depth=args.depth, build_log=args.build_log,
                                run_log=args.run_log, team_id=args.team_id,
                                repository_url=args.repository_url or args.url,
                                repository_ref=args.repository_ref)
    finally:
        if not args.keep_intermediates:
            _cleanup_tree_intermediates(output_file)
            print("[cleanup] 已删除 tree、digest 和模型工作目录，仅保留 HTML 报告")
    if result is None:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
