# -*- coding: utf-8 -*-
"""单队伍对比报告（复用确定性产物、跳过 AI 检测）与一页摘要的重跑驱动。

用于描述/开发报告已发布，仅对比报告失败（如语义分析输出截断等报告期门禁问题）
修复后只重跑对比 + 摘要，不触碰已完成的描述与开发：
  python rerun_compare_summary.py --team T202610486999578-173 \
      --url https://gitlab.eduxiji.net/educg-group-43501-3132633/T202610486999578-173

- 对比报告：--skip-ai-detect --resume-from report，复用 fastpath/recall/exact/segment/
  metadata 产物，跳过嵌入/检测模型加载，只跑报告阶段；前序产物缺失则降级完整重跑。
- 一页摘要：用新对比 digest + 既有描述/开发 digest 重出，发布到正式目录。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from oskernel_agent.cli import batch as B


def log(msg: str) -> None:
    print(f"[compare+summary] {msg}", flush=True)


def retry(label: str, thunk, attempts: int = 3) -> bool:
    for attempt in range(1, attempts + 1):
        ok, body = thunk()
        if ok:
            return True
        log(f"  {label} 失败（第 {attempt}/{attempts} 次）：{str(body)[:300]}")
        time.sleep(20 * attempt)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="对比报告+一页摘要重跑")
    parser.add_argument("--team", required=True)
    parser.add_argument("--url", required=True)
    args = parser.parse_args()

    load_dotenv(B.ROOT / ".env", override=False)
    team_id, url = args.team, args.url
    repo_name = B.fork_to_repo_name(url)
    work_dir = B.OUT / "_incremental" / team_id
    final_dir = B.OUT / team_id
    work_dir.mkdir(parents=True, exist_ok=True)
    final_dir.mkdir(parents=True, exist_ok=True)

    if not B.ensure_clone(url, repo_name):
        log("克隆最终失败，无法继续")
        return 1

    def comparison() -> tuple[bool, str]:
        cmd = [B.PY, "-m", "oskernel_agent.comparison.pipeline",
               "--repo", url + ".git", "--baselines",
               "--skip-ai-detect", "--resume-from", "report"]
        ok, body = B.run_step("对比报告(续跑)", cmd,
                              B.LOGDIR / f"{team_id}_comparison.log", timeout=3600)
        if not ok:
            return False, body
        for pair in (
            (
                B.OUT / repo_name / f"{repo_name}_comparison.html",
                B.OUT / repo_name / f"{repo_name}_comparison.digest.json",
            ),
            (
                B.OUT / f"{repo_name}_comparison.html",
                B.OUT / f"{repo_name}_comparison.digest.json",
            ),
        ):
            if pair[0].is_file() and pair[1].is_file():
                dst = work_dir / "comparison.html"
                dst_digest = work_dir / "comparison.digest.json"
                shutil.copy2(pair[0], dst)
                shutil.copy2(pair[1], dst_digest)
                B.normalize_comparison_identity(dst, dst_digest, team_id, repo_name)
                return True, body
        return False, body + "\n对比报告命令虽返回成功，但未产生本轮新的 HTML 与摘要文件。"

    ok = retry("对比报告", comparison)
    if ok:
        for suffix in (".html", ".digest.json"):
            shutil.copy2(work_dir / f"comparison{suffix}",
                         final_dir / f"comparison{suffix}")
        log("对比报告 成功 → 已发布")
    else:
        log("对比报告 最终失败，无法继续摘要")
        return 1

    digests = {k: work_dir / f"{k}.digest.json"
               for k in ("comparison", "description", "development")}
    if all(p.is_file() for p in digests.values()):
        lf = B.LOGDIR / f"{team_id}_summary.log"
        ok = retry("一页摘要", lambda: B.do_summary(team_id, work_dir, lf))
        if ok:
            shutil.copy2(work_dir / "summary.pdf", final_dir / "summary.pdf")
            log("一页摘要 成功 → 已发布")
        else:
            log("一页摘要 最终失败")
    else:
        missing = [name for name, p in digests.items() if not p.is_file()]
        log(f"一页摘要 跳过：缺少输入 digest（{missing}）")

    for name in ("summary.pdf", "description.html", "development.html", "comparison.html"):
        print(f"REPORT {name}={'ok' if (final_dir / name).is_file() else 'missing'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
