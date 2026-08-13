# -*- coding: utf-8 -*-
"""单队伍描述报告与一页摘要重出驱动（不分析编译/真实可用性）。

用法：
  python rerun_desc_summary.py --team T2026104869910383-1931 \
      --url https://gitlab.eduxiji.net/educg-group-43501-3132633/T2026104869910383-1931
  python rerun_desc_summary.py --team ... --url ... --summary-only   # 只重出摘要

描述报告重算事实（硬编码扫描，不再执行编译）并重新生成 tree.json / digest / HTML；
摘要用新的描述 digest + 既有对比与开发 digest 重出。成功后发布到正式目录，
不触碰对比与开发报告。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time

from dotenv import load_dotenv

from oskernel_agent.cli import batch as B


def log(msg: str) -> None:
    print(f"[rerun] {msg}", flush=True)


def retry(label: str, thunk, attempts: int = 3) -> bool:
    for attempt in range(1, attempts + 1):
        ok, body = thunk()
        if ok:
            return True
        log(f"  {label} 失败（第 {attempt}/{attempts} 次）：{str(body)[:300]}")
        time.sleep(20 * attempt)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="单队伍描述+摘要重出（无编译分析）")
    parser.add_argument("--team", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--summary-only", action="store_true",
                        help="描述报告已重出时只重出一页摘要")
    args = parser.parse_args()

    load_dotenv(B.ROOT / ".env", override=False)
    team_id, url = args.team, args.url
    work_dir = B.OUT / "_incremental" / team_id
    final_dir = B.OUT / team_id
    work_dir.mkdir(parents=True, exist_ok=True)
    final_dir.mkdir(parents=True, exist_ok=True)

    if not args.summary_only:
        lf = B.LOGDIR / f"{team_id}_description.log"
        ok = retry("描述报告", lambda: B.do_description(team_id, url, work_dir, lf))
        if ok:
            shutil.copy2(work_dir / "description.html", final_dir / "description.html")
            shutil.copy2(work_dir / "description.digest.json", final_dir / "description.digest.json")
            log("描述报告 成功 → 已发布")
        else:
            log("描述报告 最终失败")
            return 1

    lf2 = B.LOGDIR / f"{team_id}_summary.log"
    ok = retry("一页摘要", lambda: B.do_summary(team_id, work_dir, lf2))
    if ok:
        shutil.copy2(work_dir / "summary.pdf", final_dir / "summary.pdf")
        log("一页摘要 成功 → 已发布")
    else:
        log("一页摘要 最终失败")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
