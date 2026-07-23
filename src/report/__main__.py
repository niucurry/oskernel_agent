"""report 模块 CLI：

  # 语义级对比报告（与 python -m src.pipeline 内部走同一函数，格式完全一致）
  python -m src.report compare \\
      --suspects data/output/xxx_suspects.json \\
      --query-repo data/historical_repos/team-xyz \\
      [--recall data/output/xxx_recall.json]

  # 预取历史仓库 HEAD sha 缓存（报告里文件链接用）
  python -m src.report gitlab-heads
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from .semantic_compare import DEFAULT_OUTPUT_DIR, run_semantic_compare

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.report", description="查重对比报告生成。")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser(
        "compare",
        help="语义级对比报告：suspects.json → opencode 分析 → 直接 HTML",
    )
    pc.add_argument("--suspects", required=True,
                    help="exact 阶段产出的 *_suspects.json 路径")
    pc.add_argument("--query-repo", default=None,
                    help="新作品本地克隆路径（用于文件链接 + opencode 读取上下文）")
    pc.add_argument("--recall", default=None,
                    help="embed 阶段产出的 *_recall.json（用于计算函数总数 / 原创函数）")
    pc.add_argument("--filematch", default=None,
                    help="fastpath 产出的 *_filematch.json（L0 整文件复制清单）")
    pc.add_argument("--db", default=str(PROJECT_ROOT / "data/db/functions.db"),
                    help="历史 functions.db（用于读取参考 repo 的函数源码）")
    pc.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                    help=f"HTML 输出目录（默认 {DEFAULT_OUTPUT_DIR}）")
    pc.add_argument("--top-per-module", type=int, default=20,
                    help="每个子模块送入语义分析的最大相似代码对数（默认 20；模块借鉴对 <20 时全部分析）")
    pc.add_argument("--skip-opencode", action="store_true",
                    help="跳过 opencode，仅用规则生成报告（调试用）")

    gh = sub.add_parser("gitlab-heads", help="预取历史仓库 HEAD sha 缓存（报告里文件链接用）")
    gh.add_argument("--repos-yaml", default="config/repos.yaml")
    gh.add_argument("--cache", default="data/db/repo_heads.json")

    audit = sub.add_parser("audit", help="审计历史库覆盖与全部交付比较报告的有效性")
    audit.add_argument("--db", default=str(PROJECT_ROOT / "data/db/functions.db"))
    audit.add_argument("--config", default=str(PROJECT_ROOT / "config/repos.yaml"))
    audit.add_argument("--reports", default=str(PROJECT_ROOT / "reports_by_work_id"))
    audit.add_argument(
        "--output", default=str(PROJECT_ROOT / "data/output/recall_completeness_audit.json"))
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)

    if args.cmd == "compare":
        if not Path(args.suspects).is_file():
            logger.error("suspects 文件不存在：{}", args.suspects)
            return 1
        try:
            result = run_semantic_compare(
                suspects_path   = args.suspects,
                query_repo_path = args.query_repo,
                recall_path     = args.recall,
                output_dir      = args.output_dir,
                top_per_module  = args.top_per_module,
                skip_opencode   = args.skip_opencode,
                filematch_path  = args.filematch,
                functions_db_path = args.db,
            )
        except RuntimeError as exc:
            logger.error("拒绝生成报告：{}", exc)
            return 2
        logger.info("对比报告 → {}", result["html_path"])
        return 0

    if args.cmd == "gitlab-heads":
        from .gitlab_links import build_repo_url_map, ensure_heads, load_heads
        url_map = build_repo_url_map(args.repos_yaml)
        if not url_map:
            logger.error("未从 {} 读到任何仓库", args.repos_yaml)
            return 1
        logger.info("共 {} 个仓库，开始/补取 HEAD sha …", len(url_map))
        ensure_heads(list(url_map.values()), args.cache)
        heads = load_heads(args.cache)
        logger.info("完成：缓存 {}/{} 个仓库 HEAD → {}", len(heads), len(url_map), args.cache)
        return 0

    if args.cmd == "audit":
        from .audit import run_audit
        try:
            result = run_audit(
                db_path=args.db,
                config_path=args.config,
                reports_root=args.reports,
                output_path=args.output,
            )
        except (OSError, ValueError) as exc:
            logger.error("审计失败：{}", exc)
            return 2
        cov = result["history_coverage"]
        reports = result["reports"]
        logger.info(
            "历史库 {}/{}；报告 {} 份，有效 {}，失效 {}，未标记 {}；审计 → {}",
            cov["covered"], cov["configured"], reports["comparison_reports"],
            reports["complete_reports"], reports["stale_reports"],
            len(reports["unmarked_reports"]), result["_output_path"],
        )
        return 0 if result["valid"] else 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
