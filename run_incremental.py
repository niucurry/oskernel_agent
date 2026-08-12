# -*- coding: utf-8 -*-
"""单队伍四报告一对一产出驱动：每一步独立跑、独立发布到正式目录。

用法：
  python run_incremental.py --team T2026104879911127-1444 \
      --url https://gitlab.eduxiji.net/educg-group-43501-3132633/T2026104879911127-1444

- 每份报告（对比 / 描述 / 开发过程 / 一页摘要）独立执行、独立发布：任一报告成功即发布
  到 data/output/<队伍编号>/，失败只影响它自身，其余步骤照常进行。
- 对比报告的 AI 检测阶段若反复失败（原生崩溃无 traceback），自动改用 --skip-ai-detect
  兜底重跑（对比报告缺 AI 检测章，但仍产出）；仍失败则该步骤标记为 skipped。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from oskernel_agent.cli import batch as B

REPORTS = ("summary.pdf", "description.html", "development.html", "comparison.html")


def log(msg: str) -> None:
    print(f"[incremental] {msg}", flush=True)


def retry(label: str, thunk, attempts: int = 3) -> bool:
    """带退避地重试单步；失败返回 False 不抛出。"""
    for attempt in range(1, attempts + 1):
        ok, body = thunk()
        if ok:
            return True
        log(f"  {label} 失败（第 {attempt}/{attempts} 次）：{str(body)[:200]}")
        time.sleep(15 * attempt)
    return False


def publish(work_dir: Path, final_dir: Path, kind: str) -> None:
    """把该报告与摘要数据复制到正式目录（不删除已发布的其他报告）。"""
    for suffix in (".html", ".digest.json"):
        src = work_dir / f"{kind}{suffix}"
        if src.is_file():
            shutil.copy2(src, final_dir / src.name)


def comparison_skip_ai(team_id: str, url: str, work_dir: Path, logfile: Path) -> tuple[bool, str]:
    """对比报告兜底：AI 检测反复失败时用 --skip-ai-detect 重跑并发布。

    前序产物齐备时用 --resume-from report 只续跑报告阶段（更快）；否则完整重跑。
    """
    repo_name = B.fork_to_repo_name(url)
    cmd = [B.PY, "-m", "oskernel_agent.comparison.pipeline",
           "--repo", url + ".git", "--baselines", "--skip-ai-detect"]
    if B._comparison_resume_ready(repo_name):
        cmd.append("--resume-from")
        cmd.append("report")
        log("  前序产物齐备，续跑报告阶段（跳过模型加载）")
    ok, body = B.run_step("对比报告(跳过AI检测)", cmd, logfile, timeout=3600)
    if not ok:
        return False, body
    source_pairs = (
        (
            B.OUT / repo_name / f"{repo_name}_comparison.html",
            B.OUT / repo_name / f"{repo_name}_comparison.digest.json",
        ),
        (
            B.OUT / f"{repo_name}_comparison.html",
            B.OUT / f"{repo_name}_comparison.digest.json",
        ),
    )
    for pair in source_pairs:
        if pair[0].is_file() and pair[1].is_file():
            dst = work_dir / "comparison.html"
            dst_digest = work_dir / "comparison.digest.json"
            shutil.copy2(pair[0], dst)
            shutil.copy2(pair[1], dst_digest)
            B.normalize_comparison_identity(dst, dst_digest, team_id, repo_name)
            return True, body
    return False, body + "\n跳过 AI 检测的对比报告未产出 HTML 与摘要文件。"


def main() -> int:
    parser = argparse.ArgumentParser(description="单队伍四报告一对一产出")
    parser.add_argument("--team", required=True, help="队伍编号，如 T2026104879911127-1444")
    parser.add_argument("--url", required=True, help="GitLab Fork 地址")
    args = parser.parse_args()

    team_id = args.team
    url = args.url
    load_dotenv(B.ROOT / ".env", override=False)
    repo_name = B.fork_to_repo_name(url)
    final_dir = B.OUT / team_id
    work_dir = B.OUT / "_incremental" / team_id
    work_dir.mkdir(parents=True, exist_ok=True)
    final_dir.mkdir(parents=True, exist_ok=True)

    if not B.ensure_clone(url, repo_name):
        log("克隆最终失败，无法继续")
        return 1

    # 对比报告：先正常路径（含 AI 检测，AI 检测本地模型崩溃已知，只试 1 次）；
    # 失败则跳过 AI 检测兜底（兜底本身带重试）。
    lf = B.LOGDIR / f"{team_id}_comparison.log"
    ok = retry("对比报告", lambda: B.do_comparison(team_id, url, work_dir, lf), attempts=1)
    if not ok:
        log("对比报告正常路径失败，改用 --skip-ai-detect 兜底")
        ok = retry("对比报告(跳过AI检测)", lambda: comparison_skip_ai(team_id, url, work_dir, lf))
    if ok:
        publish(work_dir, final_dir, "comparison")
        log("对比报告 成功 → 已发布")
    else:
        log("对比报告 最终失败，标记为跳过；其余步骤不受影响")

    steps = (
        ("描述报告", "description", lambda lf: B.do_description(team_id, url, work_dir, lf)),
        ("开发过程报告", "development", lambda lf: B.do_development(team_id, url, work_dir, lf)),
    )
    for label, kind, fn in steps:
        lf = B.LOGDIR / f"{team_id}_{kind}.log"
        ok = retry(label, lambda lf=lf, fn=fn: fn(lf))
        if ok:
            publish(work_dir, final_dir, kind)
            log(f"{label} 成功 → 已发布")
        else:
            log(f"{label} 最终失败，标记为跳过")

    digests = {k: work_dir / f"{k}.digest.json" for k in ("comparison", "description", "development")}
    if all(p.is_file() for p in digests.values()):
        lf = B.LOGDIR / f"{team_id}_summary.log"
        ok = retry("一页摘要", lambda: B.do_summary(team_id, work_dir, lf))
        if ok:
            shutil.copy2(work_dir / "summary.pdf", final_dir / "summary.pdf")
            log("一页摘要 成功 → 已发布")
        else:
            log("一页摘要 最终失败，标记为跳过")
    else:
        missing = [name for name, p in digests.items() if not p.is_file()]
        log(f"一页摘要 跳过：缺少输入 digest（{missing}）")

    present = [name for name in REPORTS if (final_dir / name).is_file()]
    log(f"正式目录当前已有 {len(present)}/{len(REPORTS)}：{present}")
    if len(present) == len(REPORTS):
        B.cleanup_final_dir(team_id, final_dir)
        log("四份报告齐全，已清理 sidecar 中间产物")
    for name in REPORTS:
        print(f"REPORT {name}={'ok' if (final_dir / name).is_file() else 'missing'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
