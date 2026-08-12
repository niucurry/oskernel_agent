# -*- coding: utf-8 -*-
"""单队伍四报告增量产出：每一步独立跑、独立发布到正式目录。

batch 的事务式发布要求四份齐全才发布；本驱动改为「一个一个产出」——
任一报告成功即发布，失败只影响它自身，其余步骤照常进行。步骤仍复用
batch 的 do_* 函数与清理门禁，未弱化任何校验。
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from oskernel_agent.cli import batch as B

TEAM_ID = "T2026104869910383-1931"
URL = "https://gitlab.eduxiji.net/educg-group-43501-3132633/T2026104869910383-1931"
REPORTS = ("summary.pdf", "description.html", "development.html", "comparison.html")


def log(msg: str) -> None:
    print(f"[incremental] {msg}", flush=True)


def retry(label: str, thunk, attempts: int = 3) -> bool:
    """带退避地重试单步；失败返回 False 不抛出。"""
    for attempt in range(1, attempts + 1):
        ok, _body = thunk()
        if ok:
            return True
        log(f"  {label} 失败（第 {attempt}/{attempts} 次），等待后重试")
        time.sleep(20 * attempt)
    return False


def publish(work_dir: Path, final_dir: Path, kind: str) -> None:
    """把该报告与摘要数据复制到正式目录（不删除已发布的其他报告）。"""
    for suffix in (".html", ".digest.json"):
        src = work_dir / f"{kind}{suffix}"
        if src.is_file():
            shutil.copy2(src, final_dir / src.name)


def main() -> int:
    load_dotenv(B.ROOT / ".env", override=False)
    repo_name = B.fork_to_repo_name(URL)
    final_dir = B.OUT / TEAM_ID
    work_dir = B.OUT / "_incremental" / TEAM_ID
    work_dir.mkdir(parents=True, exist_ok=True)
    final_dir.mkdir(parents=True, exist_ok=True)

    if not B.ensure_clone(URL, repo_name):
        log("克隆最终失败，无法继续")
        return 1

    steps = (
        ("对比报告", "comparison", lambda lf: B.do_comparison(TEAM_ID, URL, work_dir, lf)),
        ("描述报告", "description", lambda lf: B.do_description(TEAM_ID, URL, work_dir, lf)),
        ("开发过程报告", "development", lambda lf: B.do_development(TEAM_ID, URL, work_dir, lf)),
    )
    for label, kind, fn in steps:
        lf = B.LOGDIR / f"{TEAM_ID}_{kind}.log"
        ok = retry(label, lambda lf=lf, fn=fn: fn(lf))
        if ok:
            publish(work_dir, final_dir, kind)
            log(f"{label} 成功 → 已发布到 {final_dir}")
        else:
            log(f"{label} 最终失败；其余步骤不受影响")

    digests = {k: work_dir / f"{k}.digest.json" for k in ("comparison", "description", "development")}
    if all(p.is_file() for p in digests.values()):
        lf = B.LOGDIR / f"{TEAM_ID}_summary.log"
        ok = retry("一页摘要", lambda: B.do_summary(TEAM_ID, work_dir, lf))
        if ok:
            shutil.copy2(work_dir / "summary.pdf", final_dir / "summary.pdf")
            log("一页摘要 成功 → 已发布")
        else:
            log("一页摘要 最终失败")
    else:
        missing = [name for name, p in digests.items() if not p.is_file()]
        log(f"一页摘要 跳过：缺少输入 digest（{missing}）")

    present = [name for name in REPORTS if (final_dir / name).is_file()]
    log(f"正式目录当前已有 {len(present)}/{len(REPORTS)}：{present}")
    if len(present) == len(REPORTS):
        B.cleanup_final_dir(TEAM_ID, final_dir)
        log("四份报告齐全，已清理 sidecar 中间产物")
    for name in REPORTS:
        print(f"REPORT {name}={'ok' if (final_dir / name).is_file() else 'missing'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
