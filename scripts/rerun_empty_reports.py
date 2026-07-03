# -*- coding: utf-8 -*-
"""重跑 reports_by_work_id 里 6 份空的对比报告（云端克隆/解析失败所致），
其中 3 个作品描述报告同样为空，一并重出。

来源修正（云端失败的根因）：
  - T2026104869910069-16      代码在 canonical 分支（默认 main 为空）
  - T2026136559910266-798     代码在 master 分支，且含 Windows 保留名文件
                              kernel/src/task/aux.rs（本地克隆已 sparse 排除该文件）
  - T2026142239910649         真实仓库名为 os-kernel-2026-xt-2026142239910649，
                              代码在 master 分支
  以上三个已按正确分支克隆到 data/output/_repos/，以本地路径喂流水线；
  其余三个默认分支即有代码，直接给 URL。

用法：python scripts/rerun_empty_reports.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
if not Path(PY).exists():
    PY = str(ROOT / ".venv" / "bin" / "python")
RBW = ROOT / "reports_by_work_id"
OUT = ROOT / "data" / "output"
REPOS = OUT / "_repos"
BAK = OUT / "_batch" / "workid_fix_bak"
LOGDIR = OUT / "_batch"
G = "https://gitlab.eduxiji.net/educg-group-43501-3132633"

# (作品目录名, 流水线 --repo 参数, 是否重出描述报告)
WORKS: list[tuple[str, str, bool]] = [
    ("T2026104609910064-854", f"{G}/T2026104609910064-854.git", False),
    ("T2026104869911125-3113", f"{G}/T2026104869911125-3113.git", False),
    ("T202610487999805-1289", f"{G}/T202610487999805-1289.git", False),
    ("T2026104869910069-16", str(REPOS / "T2026104869910069-16"), True),
    ("T2026136559910266-798", str(REPOS / "T2026136559910266-798"), True),
    ("T2026142239910649", str(REPOS / "os-kernel-2026-xt-2026142239910649"), True),
]


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def child_env() -> dict:
    env = dict(os.environ)
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    env.setdefault("HF_HUB_OFFLINE", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def run_step(name: str, cmd: list[str], logfile: Path, timeout: int) -> bool:
    log(f"  {name}: {' '.join(cmd)}")
    with logfile.open("w", encoding="utf-8", errors="replace") as lf:
        lf.write(f"# {' '.join(cmd)}\n# start {datetime.now()}\n\n")
        lf.flush()
        try:
            p = subprocess.run(cmd, cwd=ROOT, env=child_env(), stdout=lf,
                               stderr=subprocess.STDOUT, text=True, timeout=timeout)
            ok = p.returncode == 0
        except subprocess.TimeoutExpired:
            lf.write("\n# TIMEOUT\n")
            ok = False
    log(f"  {name}: {'OK' if ok else 'FAIL'}（日志 {logfile.name}）")
    return ok


def archive(src: Path, work: str, kind: str) -> bool:
    """把新产物换进 reports_by_work_id，旧文件备份（不覆盖已有备份）。"""
    if not src.exists():
        log(f"  归档失败：{src} 不存在")
        return False
    dst = RBW / work / f"{kind}.html"
    bak = BAK / work / f"{kind}.html"
    if dst.exists() and not bak.exists():
        bak.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dst, bak)
    shutil.copy2(src, dst)
    log(f"  已归档 → {dst}")
    return True


def main() -> None:
    only = sys.argv[1:]
    n_ok = 0
    for work, repo_arg, redo_desc in WORKS:
        if only and work not in only:
            continue
        log(f"=== {work}")
        repo_name = Path(repo_arg).name.removesuffix(".git")
        # 1) 对比报告
        ok = run_step("对比报告",
                      [PY, "-m", "src.pipeline", "--repo", repo_arg, "--baselines"],
                      LOGDIR / f"redo_{work}_comparison.log",
                      timeout=int(os.environ.get("BATCH_CMP_TIMEOUT", "7200")))
        html = OUT / repo_name / f"{repo_name}_comparison.html"
        if not html.exists():
            alt = OUT / f"{repo_name}_comparison.html"
            html = alt if alt.exists() else html
        if ok or html.exists():
            if archive(html, work, "comparison"):
                n_ok += 1
        # 2) 描述报告（仅空的那三个）
        if redo_desc:
            dst_tmp = OUT / repo_name / f"{repo_name}_description.html"
            repo_path = repo_arg if not repo_arg.startswith("http") else None
            src_arg = ["--repo-path", repo_path] if repo_path else ["--url", repo_arg]
            ok2 = run_step("描述报告",
                           [PY, str(ROOT / "agent.py"), *src_arg, "-o", str(dst_tmp)],
                           LOGDIR / f"redo_{work}_description.log", timeout=3600)
            if ok2 or dst_tmp.exists():
                archive(dst_tmp, work, "description")
    log(f"完成，对比报告归档 {n_ok}/{len(only) if only else len(WORKS)}")


if __name__ == "__main__":
    main()
