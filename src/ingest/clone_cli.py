"""供前端调用的安全 Git 克隆入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .cloner import clone_repo


def main() -> None:
    parser = argparse.ArgumentParser(description="克隆并准备可分析的 Git 工作区")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--depth", type=int, default=200)
    args = parser.parse_args()
    status = clone_repo(args.repo, Path(args.dest), depth=args.depth)
    print(json.dumps({"status": status, "path": str(Path(args.dest).resolve())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
