"""python -m oskernel_agent.report_jobs — single entry point for report generation."""

from __future__ import annotations

import argparse
import json
import sys

from ._runner import run

ALL_KINDS = ["comparison", "description", "development", "summary"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m oskernel_agent.report_jobs",
        description="统一报告生成入口 — 按依赖顺序编排全部四件套。",
    )
    p.add_argument("--repo", required=True, help="仓库 URL 或本地路径")
    p.add_argument("--repo-id", required=True, help="作品标识符")
    p.add_argument("--output-dir", required=True, help="输出目录")
    p.add_argument(
        "--kinds", default=",".join(ALL_KINDS),
        help=f"逗号分隔的报告类型（{' / '.join(ALL_KINDS)}），默认全部",
    )
    p.add_argument("--no-baselines", action="store_true",
                   help="comparison 不启用上游基线扣除")
    p.add_argument("--min-commits", type=int, default=None,
                   help="development 的最低提交次数阈值")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip() in ALL_KINDS]
    if not kinds:
        print("[错误] --kinds 为空或全部无效", file=sys.stderr)
        return 2

    result = run(
        args.repo, args.repo_id, args.output_dir, kinds,
        baselines=not args.no_baselines,
        min_commits=args.min_commits,
    )
    print(json.dumps({
        "repo_id": result.repo_id,
        "kinds": {
            k: {
                "status": v.status,
                "html_path": v.html_path,
                "digest_path": v.digest_path,
                "error": v.error,
            }
            for k, v in result.kinds.items()
        },
        "started_at": result.started_at,
        "finished_at": result.finished_at,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
