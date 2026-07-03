# -*- coding: utf-8 -*-
"""重跑仍带「疑似借鉴（待复核）」档的对比报告，使其经现行流水线的 LLM 语义复核，
与 作品.txt 前 50 份口径一致（review 档被裁决为 高度疑似借鉴/自研原创，不再展示待复核）。

任务清单在启动时扫描 reports_by_work_id 现场生成：报告里 review KPI>0 或模块 chip>0
即入列。重跑成功后新报告不再带该档 → 下次启动自然跳过（断点续跑）；复核后仍有
残留（复核失败函数）的作品记入 state 不再重试。

仓库地址从该作品报告里的 gitlab blob 链接反推；无链接的按
educg-group-43501-3132633/<作品目录名> 拼接。云端能产出这些报告说明默认分支有代码，
直接给 URL 让流水线自克隆（已预克隆的会复用）。每份跑完删除克隆省磁盘。

用法：
    python scripts/rerun_review_tier.py            # 全部
    python scripts/rerun_review_tier.py --limit 10 # 只跑前 10 份（按待复核数降序）
    python scripts/rerun_review_tier.py <作品目录名...>
"""
from __future__ import annotations

import json
import os
import re
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
BAK = OUT / "_batch" / "workid_fix_bak"
LOGDIR = OUT / "_batch"
STATE = LOGDIR / "rerun_review_state.json"
G = "https://gitlab.eduxiji.net/educg-group-43501-3132633"

KPI_RE = re.compile(r'>(\d+)</span><span class="l">疑似借鉴（待复核）（函数）')
CHIP_RE = re.compile(r'text-amber-700">疑似借鉴（待复核） \d+%')


def log(msg: str) -> None:
    print(f"[{datetime.now():%m-%d %H:%M:%S}] {msg}", flush=True)


def review_count(html: str) -> int:
    m = KPI_RE.search(html)
    return (int(m.group(1)) if m else 0) + len(CHIP_RE.findall(html))


def scan_targets() -> list[tuple[str, int]]:
    """返回 [(作品目录名, 待复核数)]，按待复核数降序。"""
    res = []
    for d in sorted(RBW.iterdir()):
        p = d / "comparison.html"
        if not d.is_dir() or not p.exists():
            continue
        n = review_count(p.read_text(encoding="utf-8", errors="replace"))
        if n > 0:
            res.append((d.name, n))
    res.sort(key=lambda x: -x[1])
    return res


def repo_url(work: str) -> str:
    for kind in ("description.html", "comparison.html"):
        p = RBW / work / kind
        if not p.exists():
            continue
        h = p.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'https://gitlab\.eduxiji\.net/([^"/]+)/([^"/]+)/-/blob/', h)
        if m:
            return f"https://gitlab.eduxiji.net/{m.group(1)}/{m.group(2)}.git"
    return f"{G}/{work}.git"


def child_env() -> dict:
    env = dict(os.environ)
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    env.setdefault("HF_HUB_OFFLINE", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"attempted": {}}


def save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def rerun_one(work: str, url: str) -> str:
    repo_name = url.rstrip("/").split("/")[-1].removesuffix(".git")
    logfile = LOGDIR / f"redo_{work}_comparison.log"
    with logfile.open("w", encoding="utf-8", errors="replace") as lf:
        lf.write(f"# {url}\n# start {datetime.now()}\n\n")
        lf.flush()
        try:
            p = subprocess.run(
                [PY, "-m", "src.pipeline", "--repo", url, "--baselines"],
                cwd=ROOT, env=child_env(), stdout=lf, stderr=subprocess.STDOUT,
                text=True, timeout=int(os.environ.get("BATCH_CMP_TIMEOUT", "7200")))
            ok = p.returncode == 0
        except subprocess.TimeoutExpired:
            lf.write("\n# TIMEOUT\n")
            ok = False
    html = OUT / repo_name / f"{repo_name}_comparison.html"
    if not html.exists():
        return "FAIL（无产物）" if not ok else "FAIL（产物缺失）"
    new = html.read_text(encoding="utf-8", errors="replace")
    resid = review_count(new)
    # 归档（旧文件备份一次）
    dst = RBW / work / "comparison.html"
    bak = BAK / work / "comparison.html"
    if dst.exists() and not bak.exists():
        bak.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dst, bak)
    shutil.copy2(html, dst)
    # 清理克隆省磁盘
    clone = OUT / "_repos" / repo_name
    if clone.exists():
        shutil.rmtree(clone, ignore_errors=True)
    return f"OK（待复核残留 {resid}）"


def main() -> None:
    args = sys.argv[1:]
    limit = None
    if "--limit" in args:
        i = args.index("--limit")
        limit = int(args[i + 1])
        args = args[:i] + args[i + 2:]
    targets = scan_targets()
    if args:
        targets = [(w, n) for w, n in targets if w in args]
    st = load_state()
    targets = [(w, n) for w, n in targets if w not in st["attempted"]]
    if limit:
        targets = targets[:limit]
    log(f"待重跑：{len(targets)} 份（按待复核数降序）")
    for i, (w, n) in enumerate(targets, 1):
        url = repo_url(w)
        log(f"[{i}/{len(targets)}] {w}（待复核 {n}）← {url}")
        res = rerun_one(w, url)
        log(f"[{i}/{len(targets)}] {w}: {res}")
        st["attempted"][w] = {"when": str(datetime.now()), "result": res}
        save_state(st)
    log("全部完成")


if __name__ == "__main__":
    main()
