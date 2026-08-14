# -*- coding: utf-8 -*-
"""从「内核赛作品仓库列表.xlsx」读取被分析仓库的位置并批量下载。

用法：
  python run_works_xlsx.py                 # 下载全部作品仓库到 data/output/_repos/
  python run_works_xlsx.py --xlsx "D:\\@MyData\\work\\OS\\内核赛作品仓库列表.xlsx"
  python run_works_xlsx.py --dry-run       # 只列出队号 / fork 地址 / 落盘位置
  python run_works_xlsx.py --jobs 4        # 4 路并行下载
  python run_works_xlsx.py --reports       # 下载后逐队调用 run_incremental.py 产出四份报告
  python run_works_xlsx.py --branch "https://gitlab.eduxiji.net/.../T2026102699911199-801=os2026-2"

- 仓库统一落到 data/output/_repos/<存储键>/：批量分析各步骤（对比 / 描述 / 开发过程）
  都从该位置读取被分析仓库，run_incremental.py 会直接复用已下载的克隆，不再重复下载。
- 队号取 fork 地址最后一段（如 T2026100069910651-2494）。
- 默认分支无源码的作品，用 --branch URL=BRANCH 选择真正含代码的分支（可重复）。
- GitHub 的 /tree/<分支>、/blob/<分支>/… 网页地址自动转为“仓库地址 + 该分支”克隆；
  无法映射的 GitHub 浏览页（如 /commit/、/pull/）跳过并告警。
- 单个仓库失败不阻断其余；结束时打印汇总。
- --reports 逐队串行执行：并发批次会拖垮内存（对比流水线会加载大模型）。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import subprocess
import sys
import urllib.parse
from pathlib import Path

from dotenv import load_dotenv

from oskernel_agent.cli import batch as B
from oskernel_agent.works_list import WorksEntry, clone_target, read_works_xlsx


def _configure_utf8_output() -> None:
    """Make redirected output readable by UTF-8 callers on Windows."""
    for stream in (sys.stdout, sys.stderr):
        if stream.isatty():
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


_configure_utf8_output()


# 默认按“项目根目录 → 上级工作区”顺序查找作品仓库列表
DEFAULT_XLSX_CANDIDATES = (
    Path("内核赛作品仓库列表.xlsx"),
    Path("..") / "内核赛作品仓库列表.xlsx",
)


def find_default_xlsx() -> Path | None:
    for candidate in DEFAULT_XLSX_CANDIDATES:
        path = (B.ROOT / candidate).resolve()
        if path.is_file():
            return path
    return None


def branch_match_key(url: str) -> str:
    """--branch 覆写匹配键：忽略大小写、尾部斜杠与 .git 后缀、查询参数。"""
    value = url.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    path = parsed.path
    if path.casefold().endswith(".git"):
        path = path[:-4]
    return f"{(parsed.hostname or '').casefold()}{path.casefold()}"


def parse_branch_overrides(specs: list[str]) -> dict[str, str] | None:
    """解析 --branch URL=BRANCH 列表；格式错误返回 None（由调用方报错退出）。"""
    overrides: dict[str, str] = {}
    for spec in specs:
        if "=" not in spec or not spec.split("=", 1)[1].strip() or not spec.split("=", 1)[0].strip():
            return None
        url, branch = spec.split("=", 1)
        overrides[branch_match_key(url)] = branch.strip()
    return overrides


def download_one(entry: WorksEntry, branch_overrides: dict[str, str] | None = None) -> dict:
    """下载单个仓库到 data/output/_repos/<存储键>/；失败返回 error 不抛出。"""
    repo_name = B.fork_to_repo_name(entry.url)
    dest = B.REPOS / repo_name
    spec = clone_target(entry.url)
    if spec is None:
        B.log(f"[works] {entry.team_id} 跳过（浏览器页面地址，非仓库地址）：{entry.url}")
        return {"team_id": entry.team_id, "url": entry.url, "dest": str(dest),
                "ok": False, "reused": False, "skipped": True}
    clone_url, branch = spec
    override = (branch_overrides or {}).get(branch_match_key(entry.url))
    if override:
        branch = override
    reused = (dest / ".git").exists()
    try:
        ok = B.ensure_clone(clone_url, repo_name, branch=branch)
        result = {"team_id": entry.team_id, "url": entry.url, "dest": str(dest),
                  "ok": ok, "reused": reused, "branch": branch}
    except Exception as exc:  # noqa: BLE001 — 单个仓库失败不阻断其余
        result = {"team_id": entry.team_id, "url": entry.url, "dest": str(dest),
                  "ok": False, "reused": reused, "branch": branch, "error": str(exc)}
    branch_note = f"（分支 {branch}）" if branch else ""
    B.log(f"[works] {entry.team_id}{branch_note} → {dest}"
          + ("" if result["ok"] else f" 失败：{result.get('error', 'clone failed')}"))
    return result


def download_all(entries: list[WorksEntry], jobs: int,
                 branch_overrides: dict[str, str] | None = None) -> list[dict]:
    if jobs <= 1 or len(entries) <= 1:
        return [download_one(entry, branch_overrides) for entry in entries]
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        return list(pool.map(lambda entry: download_one(entry, branch_overrides), entries))


def report_ok(entry: WorksEntry) -> bool:
    """逐队串行调用 run_incremental.py 产出四份报告；返回该队是否全部成功。"""
    cmd = [sys.executable, str(B.ROOT / "run_incremental.py"),
           "--team", entry.team_id, "--url", entry.url]
    B.log(f"[works] 报告 {entry.team_id}: {' '.join(cmd)}")
    completed = subprocess.run(cmd, cwd=B.ROOT, check=False)
    return completed.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="读取作品仓库列表并批量下载被分析仓库")
    parser.add_argument("--xlsx", default=None,
                        help="作品仓库列表 xlsx 路径（默认查找项目根目录及上级工作区）")
    parser.add_argument("--jobs", type=int, default=4, help="并行下载路数（默认 4）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只列出队号与目标路径，不下载")
    parser.add_argument("--reports", action="store_true",
                        help="下载后逐队调用 run_incremental.py 产出四份报告（串行）")
    parser.add_argument("--branch", action="append", default=[], metavar="URL=BRANCH",
                        help="为指定仓库选择克隆分支（可重复），如 "
                             "--branch https://gitlab.eduxiji.net/.../T2026102699911199-801=os2026-2")
    args = parser.parse_args()

    branch_overrides = parse_branch_overrides(args.branch)
    if branch_overrides is None:
        print("--branch 格式应为 URL=BRANCH（等号两侧均不能为空）", file=sys.stderr)
        return 2

    xlsx_path = Path(args.xlsx).resolve() if args.xlsx else find_default_xlsx()
    if xlsx_path is None:
        print("未找到作品仓库列表：请用 --xlsx 指定「内核赛作品仓库列表.xlsx」的路径",
              file=sys.stderr)
        return 2

    load_dotenv(B.ROOT / ".env", override=False)
    try:
        entries = read_works_xlsx(xlsx_path)
    except (FileNotFoundError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not entries:
        print(f"{xlsx_path} 中没有读取到任何 fork 地址", file=sys.stderr)
        return 2

    print(f"作品仓库列表：{xlsx_path}，共 {len(entries)} 个仓库 → {B.REPOS}")
    if args.dry_run:
        for entry in entries:
            spec = clone_target(entry.url)
            branch = None if spec is None else spec[1]
            branch = (branch_overrides or {}).get(branch_match_key(entry.url), branch)
            branch_note = f"\t分支={branch}" if branch else ""
            print(f"{entry.team_id}\t{entry.url}\t→ {B.REPOS / B.fork_to_repo_name(entry.url)}{branch_note}")
        return 0

    results = download_all(entries, max(args.jobs, 1), branch_overrides)
    ok = sum(1 for r in results if r["ok"])
    skipped = sum(1 for r in results if r.get("skipped"))
    failed = len(results) - ok - skipped
    reused = sum(1 for r in results if r.get("reused") and r["ok"])
    print(f"下载完成：成功 {ok}（其中复用已有克隆 {reused}）/ 跳过 {skipped} / 失败 {failed} / 共 {len(results)}")
    for r in results:
        if r.get("skipped"):
            print(f"  跳过：{r['team_id']} {r['url']}（浏览器页面地址，非仓库地址）")
        elif not r["ok"]:
            print(f"  失败：{r['team_id']} {r['url']} → {r.get('error', 'clone failed')}")

    exit_code = 0 if failed == 0 else 1
    if args.reports and failed == 0:
        team_failed = sum(1 for entry in entries if not report_ok(entry))
        print(f"报告产出完成：失败队伍 {team_failed} / 共 {len(entries)}")
        exit_code = 1 if team_failed else exit_code
    elif args.reports:
        print("存在下载失败的仓库，跳过报告阶段；修复后重跑（已下载的仓库会复用）")
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
