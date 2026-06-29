"""report 模块 CLI：

  # 新：语义级对比报告（直接 HTML，无需 LLM review 步骤）
  python -m src.report compare \\
      --suspects data/output/xxx_suspects.json \\
      --query-repo data/historical_repos/team-xyz \\
      [--recall data/output/xxx_recall.json]

  # 旧：从 reviewed/suspects + recall 生成报告（保留兼容）
  python -m src.report report --reviewed X_reviewed.json --recall X_recall.json

  # 历史作品档案
  python -m src.report profile --all
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from src.normalize.store import DEFAULT_DB

from .generate import DEFAULT_OUTPUT_DIR, run_report
from .profile import DEFAULT_PROFILE_DIR, DEFAULT_REPOS_ROOT, run_profiles
from .semantic_compare import run_semantic_compare


def _maybe_client(no_llm: bool):
    if no_llm:
        return None
    from src.review.config import load_llm_settings
    from src.review.llm import OpenAICompatClient

    s = load_llm_settings()
    if not s.api_key:
        logger.warning("未设置 LLM_API_KEY，报告将用模板兜底（不调用 LLM）")
        return None
    return OpenAICompatClient(s)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.report", description="查重报告 / 历史作品档案生成。")
    p.add_argument("--no-llm", action="store_true", help="不调用 LLM，用模板兜底")
    sub = p.add_subparsers(dest="cmd", required=True)

    # ── 新命令：语义级对比报告（直接 HTML，不经 LLM review 步骤）──────────────
    pc = sub.add_parser(
        "compare",
        help="语义级对比报告：suspects.json → opencode 分析 → 直接 HTML（无需 review 步骤）",
    )
    pc.add_argument("--suspects", required=True,
                    help="exact 阶段产出的 *_suspects.json 路径")
    pc.add_argument("--query-repo", default=None,
                    help="新作品本地克隆路径（用于文件链接 + opencode 读取上下文）")
    pc.add_argument("--recall", default=None,
                    help="embed 阶段产出的 *_recall.json（用于计算函数总数 / 原创函数）")
    pc.add_argument("--filematch", default=None,
                    help="fastpath 产出的 *_filematch.json（L0 整文件复制清单）")
    pc.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                    help=f"HTML 输出目录（默认 {DEFAULT_OUTPUT_DIR}）")
    pc.add_argument("--top-per-module", type=int, default=20,
                    help="每个子模块送入语义分析的最大相似代码对数（默认 20；模块借鉴对 <20 时全部分析）")
    pc.add_argument("--skip-opencode", action="store_true",
                    help="跳过 opencode，仅用规则生成报告（调试用）")

    # ── 旧命令：保留兼容 ────────────────────────────────────────────────────────
    pr = sub.add_parser("report", help="从 reviewed/suspects + recall 生成报告（旧流程）")
    pr.add_argument("--reviewed", required=True, help="*_reviewed.json 或 *_suspects_final.json")
    pr.add_argument("--recall", required=True, help="*_recall.json（用于创新点章节）")
    pr.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    pr.add_argument("--repo-root", default=None,
                    help="新作品本地克隆根目录（构建 GitLab 跳转链接用）")

    pp = sub.add_parser("profile", help="为历史仓库生成作品档案")
    pp.add_argument("--all", action="store_true", help="为 functions.db 中全部仓库生成")
    pp.add_argument("--db", default=DEFAULT_DB)
    pp.add_argument("--repos-root", default=DEFAULT_REPOS_ROOT)
    pp.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR)

    gh = sub.add_parser("gitlab-heads", help="预取历史仓库 HEAD sha 缓存（报告里文件链接用）")
    gh.add_argument("--repos-yaml", default="config/repos.yaml")
    gh.add_argument("--cache", default="data/db/repo_heads.json")
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)

    # 新：语义级对比报告
    if args.cmd == "compare":
        if not Path(args.suspects).is_file():
            logger.error("suspects 文件不存在：{}", args.suspects)
            return 1
        result = run_semantic_compare(
            suspects_path   = args.suspects,
            query_repo_path = args.query_repo,
            recall_path     = args.recall,
            output_dir      = args.output_dir,
            top_per_module  = args.top_per_module,
            skip_opencode   = args.skip_opencode,
            filematch_path  = args.filematch,
        )
        logger.info("对比报告 → {}", result["html_path"])
        return 0

    client = _maybe_client(args.no_llm)

    if args.cmd == "report":
        for f in (args.reviewed, args.recall):
            if not Path(f).is_file():
                logger.error("文件不存在：{}", f)
                return 1
        run_report(args.reviewed, args.recall, client=client, output_dir=args.output_dir,
                   query_repo_path=args.repo_root)
        return 0

    if args.cmd == "profile":
        if not args.all:
            logger.error("请使用 --all")
            return 1
        res = run_profiles(args.db, repos_root=args.repos_root, profile_dir=args.profile_dir, client=client)
        logger.info("档案生成完成：{}", res)
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
    return 1


if __name__ == "__main__":
    sys.exit(main())
