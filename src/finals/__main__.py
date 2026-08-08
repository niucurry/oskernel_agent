"""决赛四件套 CLI。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m finals")
    commands = parser.add_subparsers(dest="command", required=True)
    development = commands.add_parser("development", help="生成开发过程分析报告")
    development.add_argument("--repo", required=True, help="本地 Git 仓库路径")
    development.add_argument("--repo-id", default="")
    development.add_argument("--output", required=True, help="输出 HTML 路径")
    summary = commands.add_parser("summary", help="生成一页 A4 决赛摘要 PDF")
    summary.add_argument("--description-digest", required=True)
    summary.add_argument("--development-digest", required=True)
    summary.add_argument("--comparison-digest", required=True)
    summary.add_argument("--repo-id", default="")
    summary.add_argument("--output", required=True, help="输出 PDF 路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "development":
        from .development import generate_development_report
        result = generate_development_report(
            Path(args.repo), Path(args.output), repo_id=args.repo_id or None,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "summary":
        from .summary_pdf import generate_summary_pdf
        result = generate_summary_pdf(
            [args.description_digest, args.development_digest, args.comparison_digest],
            args.output, repo_id=args.repo_id or None,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
