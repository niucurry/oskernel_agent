"""
离线构建参考 OS 代码指纹库。

对每个参考 OS 运行一次，生成 resources/reference_db/<name>.json。
正常运行无需手工调用：compare_with_reference_os 会校验指纹库，并在缺失或损坏时
根据 config/reference_sources.yaml 自动重建。本脚本仅用于主动重建或开发调试。

用法：
  python scripts/build_reference_db.py \\
      --reference rcore-tutorial-v3 \\
      --repo-path /path/to/rCore-Tutorial-v3

  # 构建全部（需要本地已克隆对应仓库）
  python scripts/build_reference_db.py --all \\
      --rcore-v3  /path/to/rCore-Tutorial-v3/os \\
      --rcore-v2  /path/to/rCore-Tutorial-v2/os \\
      --xv6       /path/to/xv6-riscv \\
      --ucore     /path/to/ucore-tutorial/os
"""

import argparse
import os
import sys

from oskernel_agent.tools.reference_db import ReferenceOSDatabase

DEFAULT_DB_DIR = "resources/reference_db"


def build_one(ref_name: str, repo_path: str, db_dir: str) -> None:
    if not os.path.isdir(repo_path):
        print(f"[错误] 路径不存在：{repo_path}")
        sys.exit(1)
    output = os.path.join(db_dir, f"{ref_name}.json")
    count = ReferenceOSDatabase.build_from_repo(ref_name, repo_path, output)
    print(f"[完成] {ref_name}：{count} 个函数 → {output}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="为参考 OS 构建代码指纹库",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--reference", "-r",
        choices=ReferenceOSDatabase.SUPPORTED,
        help="要构建的参考 OS 名称",
    )
    parser.add_argument(
        "--repo-path", "-p",
        help="参考 OS 的本地代码路径",
    )
    parser.add_argument(
        "--db-dir",
        default=DEFAULT_DB_DIR,
        help=f"指纹库输出目录（默认：{DEFAULT_DB_DIR}）",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="构建所有参考 OS（需同时提供各 --repo-* 参数）",
    )
    # 批量构建时各 OS 的路径
    parser.add_argument("--rcore-v3",  default="", help="rcore-tutorial-v3 路径")
    parser.add_argument("--rcore-v2",  default="", help="rcore-tutorial-v2 路径")
    parser.add_argument("--xv6",       default="", help="xv6-riscv 路径")
    parser.add_argument("--ucore",     default="", help="ucore 路径")

    args = parser.parse_args()

    if args.all:
        mapping = {
            "rcore-tutorial-v3": args.rcore_v3,
            "rcore-tutorial-v2": args.rcore_v2,
            "xv6-riscv":         args.xv6,
            "ucore":             args.ucore,
        }
        for ref_name, path in mapping.items():
            if path:
                build_one(ref_name, path, args.db_dir)
            else:
                print(f"[跳过] {ref_name}：未提供路径")
    elif args.reference and args.repo_path:
        build_one(args.reference, args.repo_path, args.db_dir)
    else:
        parser.print_help()
        print("\n示例：")
        print(
            "  python scripts/build_reference_db.py "
            "-r rcore-tutorial-v3 -p /path/to/rCore-Tutorial-v3/os"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
