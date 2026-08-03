"""ingest 模块 CLI 入口：python -m src.ingest --config config/repos.yaml [--force]"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from .config import write_template
from .runner import ingest

DEFAULT_CONFIG = "config/repos.yaml"
DEFAULT_REPOS_ROOT = "data/repos"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.ingest",
        description="从 GitLab 批量克隆历史作品。",
    )
    p.add_argument("--config", default=DEFAULT_CONFIG, help=f"repos.yaml 路径（默认 {DEFAULT_CONFIG}）")
    p.add_argument("--repos-root", default=DEFAULT_REPOS_ROOT, help=f"克隆输出根目录（默认 {DEFAULT_REPOS_ROOT}）")
    p.add_argument("--force", action="store_true", help="强制重新克隆已存在的仓库")
    p.add_argument(
        "--init-template",
        action="store_true",
        help="生成含 3 条示例数据的 repos.yaml 模板后退出",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)

    if args.init_template:
        path = write_template(args.config)
        logger.info("已生成模板：{}", path)
        return 0

    config_path = Path(args.config)
    if not config_path.exists():
        path = write_template(config_path)
        logger.warning("未找到配置，已生成示例模板：{}（请填入真实仓库后重跑）", path)
        return 1

    token = os.getenv("GITLAB_TOKEN")
    if not token:
        logger.warning("未设置 GITLAB_TOKEN，将以匿名方式访问（仅公开仓库，且受速率限制）")

    ingest(
        config_path,
        args.repos_root,
        token=token,
        force=args.force,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
